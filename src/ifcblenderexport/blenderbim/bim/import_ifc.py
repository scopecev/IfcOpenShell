import ifcopenshell
import ifcopenshell.geom
import bpy
import os
import json
import time
import mathutils
import math
import multiprocessing
import zipfile
import tempfile
from pathlib import Path
from .helper import SIUnitHelper
from . import schema
from . import ifc

class MaterialCreator():
    def __init__(self, ifc_import_settings):
        self.mesh = None
        self.materials = {}
        self.parsed_meshes = []
        self.ifc_import_settings = ifc_import_settings

    def create(self, element, obj, mesh):
        self.obj = obj
        self.mesh = mesh
        if not element.Representation:
            return
        if self.ifc_import_settings.should_treat_styled_item_as_material \
                and self.mesh.name in self.parsed_meshes:
            return
        if self.parse_representations(element):
            self.assign_material_slots_to_faces(obj, self.mesh)
            self.parsed_meshes.append(self.mesh.name)
            return # styled items override material styles
        self.parse_material(element)
        self.parsed_meshes.append(self.mesh.name)

    def parse_representations(self, element):
        has_parsed = False
        for representation in element.Representation.Representations:
            if self.parse_representation(representation):
                has_parsed = True
        return has_parsed

    def parse_representation(self, representation):
        has_parsed = False
        representation_items = self.resolve_mapped_representation_items(representation)
        for item in representation_items:
            if self.parse_representation_item(item):
                has_parsed = True
        return has_parsed

    def parse_representation_item(self, item):
        if not item.StyledByItem:
            return
        styled_item = item.StyledByItem[0]

        material_name = self.get_material_name(styled_item)

        if material_name not in self.materials:
            self.materials[material_name] = bpy.data.materials.new(material_name)
            self.parse_styled_item(styled_item, self.materials[material_name])

        if self.ifc_import_settings.should_treat_styled_item_as_material:
            # Revit workaround: since Revit exports all material
            # assignments as individual object styled items. Treating
            # them as reusable materials makes things much more
            # efficient in Blender.
            self.assign_material_to_mesh(self.materials[material_name])
        else:
            # Proper behaviour
            self.assign_material_to_mesh(self.materials[material_name], is_styled_item=True)
        return True

    def assign_material_slots_to_faces(self, obj, mesh):
        if not mesh['ios_materials']:
            return
        slots = [s.name for s in obj.material_slots]
        for index, polygon in enumerate(mesh.polygons):
            material = mesh['ios_materials'][mesh['ios_material_ids'][index]]
            if 'surface-style-' in material:
                material = material[len('surface-style-'):]
            try:
                polygon.material_index = slots.index(material)
            except:
                self.ifc_import_settings.logger.error(
                        'Failed to assign material {} to object {}'.format(material, obj.name))

    def parse_material(self, element):
        for association in element.HasAssociations:
            if association.is_a('IfcRelAssociatesMaterial'):
                material_select = association.RelatingMaterial
                if material_select.is_a('IfcMaterialDefinition'):
                    self.create_definition(material_select)

    def create_definition(self, material):
        if material.is_a('IfcMaterial'):
            self.create_single(material)

    def create_single(self, material):
        if material.Name not in self.materials:
            self.create_new_single(material)
        return self.assign_material_to_mesh(self.materials[material.Name])

    def create_new_single(self, material):
        self.materials[material.Name] = bpy.data.materials.new(material.Name)
        if not material.HasRepresentation \
            or not material.HasRepresentation[0].Representations:
            return
        for representation in material.HasRepresentation[0].Representations:
            if not representation.Items:
                continue
            for item in representation.Items:
                if not item.is_a('IfcStyledItem'):
                    continue
                self.parse_styled_item(item, self.materials[material.Name])

    def get_material_name(self, styled_item):
        if styled_item.Name:
            return styled_item.Name
        styled_item = self.resolve_presentation_style_assignment(styled_item)
        for style in styled_item.Styles:
            if not style.is_a('IfcSurfaceStyle'):
                continue
            if style.Name:
                return style.Name
            return str(style.id())
        return str(styled_item.id())

    def parse_styled_item(self, styled_item, material):
        styled_item = self.resolve_presentation_style_assignment(styled_item)
        for style in styled_item.Styles:
            if not style.is_a('IfcSurfaceStyle'):
                continue
            external_style = None
            for surface_style in style.Styles:
                if surface_style.is_a('IfcSurfaceStyleShading'):
                    alpha = 1.
                    # Transparency was added in IFC4
                    if hasattr(surface_style, 'Transparency') \
                            and surface_style.Transparency:
                        alpha = 1 - surface_style.Transparency
                    material.diffuse_color = (
                        surface_style.SurfaceColour.Red,
                        surface_style.SurfaceColour.Green,
                        surface_style.SurfaceColour.Blue,
                        alpha)
                elif surface_style.is_a('IfcExternallyDefinedSurfaceStyle'):
                    external_style = surface_style
            if external_style:
                material.BIMMaterialProperties.is_external = True
                material.BIMMaterialProperties.location = external_style.Location
                material.BIMMaterialProperties.identification = external_style.Identification
                material.BIMMaterialProperties.name = external_style.Name

    # IfcPresentationStyleAssignment is deprecated as of IFC4
    # However it is still widely used thanks to Revit :(
    def resolve_presentation_style_assignment(self, styled_item):
        for style in styled_item.Styles:
            if style.is_a('IfcPresentationStyleAssignment'):
                return style
        return styled_item

    def resolve_mapped_representation_items(self, representation):
        items = []
        for item in representation.Items:
            if item.is_a('IfcMappedItem'):
                items.extend(item.MappingSource.MappedRepresentation.Items)
            else:
                items.append(item)
        return items

    def assign_material_to_mesh(self, material, is_styled_item=False):
        self.mesh.materials.append(material)
        if is_styled_item:
            index = len(self.obj.material_slots) - 1
            self.obj.material_slots[index].link = 'OBJECT'
            self.obj.material_slots[index].material = material

class IfcImporter():
    def __init__(self, ifc_import_settings):
        self.ifc_import_settings = ifc_import_settings
        self.diff = None
        self.file = None
        self.settings = ifcopenshell.geom.settings()
        if self.ifc_import_settings.should_import_curves:
            self.settings.set(self.settings.INCLUDE_CURVES, True)
        self.settings_2d = ifcopenshell.geom.settings()
        self.settings_2d.set(self.settings_2d.INCLUDE_CURVES, True)
        self.project = None
        self.spatial_structure_elements = {}
        self.elements = {}
        self.type_products = {}
        self.meshes = {}
        self.mesh_shapes = {}
        self.time = 0
        self.unit_scale = 1
        self.added_data = {}
        self.native_data = {}
        self.groups = {}

        self.material_creator = MaterialCreator(ifc_import_settings)

    def execute(self):
        self.load_diff()
        self.load_file()
        self.set_ifc_file()
        if self.ifc_import_settings.should_auto_set_workarounds:
            self.auto_set_workarounds()
        self.calculate_unit_scale()
        self.create_project()
        self.create_spatial_hierarchy()
        if self.ifc_import_settings.should_import_aggregates:
            self.create_aggregates()
        if self.ifc_import_settings.should_import_opening_elements:
            self.create_openings_collection()
        self.purge_diff()
        self.parse_native_products()
        self.filter_ifc()
        self.patch_ifc()
        self.create_groups()
        self.create_type_products()
        self.create_grids()
        # TODO: Deprecate after bug #682 is fixed and the new importer is stable
        if self.ifc_import_settings.should_use_legacy:
            self.create_products_legacy()
        else:
            self.create_products()
        self.place_objects_in_spatial_tree()
        if self.ifc_import_settings.should_merge_by_class:
            self.merge_by_class()
        elif self.ifc_import_settings.should_merge_by_material:
            self.merge_by_material()
        if self.ifc_import_settings.should_clean_mesh:
            self.clean_mesh()

    def auto_set_workarounds(self):
        applications = self.file.by_type('IfcApplication')
        if not applications:
            return
        if applications[0].ApplicationIdentifier == 'Revit':
            self.ifc_import_settings.should_treat_styled_item_as_material = True
            if self.is_ifc_class_far_away('IfcSite'):
                self.ifc_import_settings.should_ignore_site_coordinates = True
            if self.is_ifc_class_far_away('IfcBuilding'):
                self.ifc_import_settings.should_ignore_building_coordinates = True
        elif applications[0].ApplicationFullName == '12D Model':
            self.ifc_import_settings.should_reset_absolute_coordinates = True
        elif applications[0].ApplicationFullName == 'Tekla Structures':
            if self.is_ifc_class_far_away('IfcSite'):
                self.ifc_import_settings.should_ignore_site_coordinates = True

    def is_ifc_class_far_away(self, ifc_class):
        for site in self.file.by_type(ifc_class):
            if not site.ObjectPlacement \
                    or not site.ObjectPlacement.RelativePlacement \
                    or not site.ObjectPlacement.RelativePlacement.Location:
                continue
            if self.is_point_far_away(site.ObjectPlacement.RelativePlacement.Location):
                return True

    def is_point_far_away(self, point):
        # Arbitrary threshold based on experience
        return abs(point.Coordinates[0]) > 1000000 \
            or abs(point.Coordinates[1]) > 1000000 \
            or abs(point.Coordinates[2]) > 1000000

    def parse_native_products(self):
        # TODO: simple code for now as we only treat rebar specially
        for element in self.file.by_type('IfcReinforcingBar'):
            for representation in element.Representation.Representations:
                self.replace_with_directrix(representation, element)
                for item in representation.Items:
                    if item.is_a('IfcMappedItem'):
                        self.replace_with_directrix(item.MappingSource.MappedRepresentation, element)
        for global_id, data in self.native_data.items():
            bevel_object = bpy.data.objects.new('rebar-profile', self.create_bezier_circle(data['radius']))
            bpy.context.scene.collection.objects.link(bevel_object)
            data['bevel_object'] = bevel_object

    def create_bezier_circle(self, radius):
        curve = bpy.data.curves.new('circle-profile', type='CURVE')
        spline = curve.splines.new('BEZIER')
        spline.use_cyclic_u = True
        spline.bezier_points.add(3)
        approx_distance = (4/3)*math.tan(math.pi/(2*4))
        for i in range(0, 4):
            spline.bezier_points[i].handle_left_type = 'AUTO'
            spline.bezier_points[i].handle_right_type = 'AUTO'
        spline.bezier_points[0].co = mathutils.Vector((-radius, 0, 0))
        spline.bezier_points[0].handle_left = mathutils.Vector((-radius, -approx_distance, 0))
        spline.bezier_points[0].handle_right = mathutils.Vector((-radius, approx_distance, 0))
        spline.bezier_points[1].co = mathutils.Vector((0, radius, 0))
        spline.bezier_points[1].handle_left = mathutils.Vector((-approx_distance, radius, 0))
        spline.bezier_points[1].handle_right = mathutils.Vector((approx_distance, radius, 0))
        spline.bezier_points[2].co = mathutils.Vector((radius, 0, 0))
        spline.bezier_points[2].handle_left = mathutils.Vector((radius, approx_distance, 0))
        spline.bezier_points[2].handle_right = mathutils.Vector((radius, -approx_distance, 0))
        spline.bezier_points[3].co = mathutils.Vector((0, -radius, 0))
        spline.bezier_points[3].handle_left = mathutils.Vector((approx_distance, -radius, 0))
        spline.bezier_points[3].handle_right = mathutils.Vector((-approx_distance, -radius, 0))
        return curve

    def replace_with_directrix(self, representation, element):
        new_items = []
        for item in representation.Items:
            if item.is_a('IfcSweptDiskSolid'):
                new_items.append(item.Directrix)
                self.native_data[element.GlobalId] = { 'radius': self.unit_scale * item.Radius }
            else:
                new_items.append(item)
        representation.Items = new_items

    def filter_ifc(self):
        for element in self.file.by_type('IfcElement'):
            if self.diff \
                    and element.GlobalId not in self.diff['added'] \
                    and element.GlobalId not in self.diff['changed'].keys():
                self.file.remove(element)

    def patch_ifc(self):
        project = self.file.by_type('IfcProject')[0]
        if self.ifc_import_settings.should_ignore_site_coordinates:
            sites = self.find_decomposed_ifc_class(project, 'IfcSite')
            for site in sites:
                self.patch_placement_to_origin(site)
        if self.ifc_import_settings.should_ignore_building_coordinates:
            buildings = self.find_decomposed_ifc_class(project, 'IfcBuilding')
            for building in buildings:
                self.patch_placement_to_origin(building)
        if self.ifc_import_settings.should_reset_absolute_coordinates:
            self.reset_absolute_coordinates()

    def reset_absolute_coordinates(self):
        # 12D can have some funky coordinates out of any sensible range. This
        # method will not work all the time, but will catch most issues.
        offset_point = None
        for point in self.file.by_type('IfcCartesianPoint'):
            if len(point.Coordinates) == 2 or not self.is_point_far_away(point):
                continue
            if not offset_point:
                offset_point = (point.Coordinates[0], point.Coordinates[1], point.Coordinates[2])
                self.ifc_import_settings.logger.info(f'Resetting absolute coordinates by {point}')
            point.Coordinates = (
                point.Coordinates[0] - offset_point[0],
                point.Coordinates[1] - offset_point[1],
                point.Coordinates[2] - offset_point[2]
            )

    def find_decomposed_ifc_class(self, element, ifc_class):
        results = []
        rel_aggregates = element.IsDecomposedBy
        if not rel_aggregates:
            return results
        for rel_aggregate in rel_aggregates:
            for part in rel_aggregate.RelatedObjects:
                if part.is_a(ifc_class):
                    results.append(part)
                results.extend(self.find_decomposed_ifc_class(part, ifc_class))
        return results

    def patch_placement_to_origin(self, element):
        element.ObjectPlacement.RelativePlacement.Location.Coordinates = (0., 0., 0.)
        if element.ObjectPlacement.RelativePlacement.Axis:
            element.ObjectPlacement.RelativePlacement.Axis.DirectionRatios = (0., 0., 1.)
        if element.ObjectPlacement.RelativePlacement.RefDirection:
            element.ObjectPlacement.RelativePlacement.RefDirection.DirectionRatios = (1., 0., 0.)

    def create_groups(self):
        collection = bpy.data.collections.new('Groups')
        self.project['blender'].children.link(collection)
        for element in self.file.by_type('IfcGroup'):
            obj = bpy.data.objects.new(f'IfcGroup/{element.Name}', None)
            self.add_element_attributes(element, obj)
            collection.objects.link(obj)
            self.groups[element.GlobalId] = {
                'ifc': element,
                'blender': obj
            }

    def create_grids(self):
        grids = self.file.by_type('IfcGrid')
        for grid in grids:
            collection = bpy.data.collections.new(self.get_name(grid))
            self.project['blender'].children.link(collection)
            element_matrix = self.get_local_placement(grid.ObjectPlacement)
            element_matrix[0][3] *= self.unit_scale
            element_matrix[1][3] *= self.unit_scale
            element_matrix[2][3] *= self.unit_scale
            self.create_grid_axes(grid.UAxes, collection, element_matrix)
            self.create_grid_axes(grid.VAxes, collection, element_matrix)

    def create_grid_axes(self, axes, grid, matrix_world):
        for axis in axes:
            shape = ifcopenshell.geom.create_shape(self.settings_2d, axis.AxisCurve)
            mesh = self.create_mesh(axis, shape)
            obj = bpy.data.objects.new(f'IfcGridAxis/{axis.AxisTag}', mesh)
            obj.matrix_world = matrix_world
            grid.objects.link(obj)

    def create_type_products(self):
        type_products = self.file.by_type('IfcTypeProduct')
        for type_product in type_products:
            self.create_type_product(type_product)

    def create_type_product(self, type_product):
        obj = bpy.data.objects.new(self.get_name(type_product), None)
        self.add_element_attributes(type_product, obj)
        self.add_element_document_relations(type_product, obj)
        self.add_type_product_psets(type_product, obj)
        self.project['blender'].objects.link(obj)
        self.type_products[type_product.GlobalId] = obj

    def create_products_legacy(self):
        elements = self.file.by_type('IfcElement') + self.file.by_type('IfcSpace')
        for element in elements:
            self.create_product_legacy(element)

    def create_products(self):
        if self.ifc_import_settings.should_use_cpu_multiprocessing:
            iterator = ifcopenshell.geom.iterator(self.settings, self.file, multiprocessing.cpu_count())
        else:
            iterator = ifcopenshell.geom.iterator(self.settings, self.file)
        valid_file = iterator.initialize()
        if not valid_file:
            self.create_products_legacy() # Sometimes, this can still work, not 100% sure why yet
            return False
        old_progress = -1
        while True:
            progress = iterator.progress() // 2
            if progress > old_progress:
                print("\r[" + "#" * progress + " " * (50 - progress) + "]", end="")
                old_progress = progress
            shape = iterator.get()
            if shape:
                self.create_product(self.file.by_id(shape.guid), shape)
            if not iterator.next():
                break
        print("\rDone creating geometry" + " " * 30)

    def create_product(self, element, shape=None):
        if element is None:
            return

        if not self.ifc_import_settings.should_import_opening_elements \
                and element.is_a('IfcOpeningElement'):
            return

        if not self.ifc_import_settings.should_import_spaces \
                and element.is_a('IfcSpace'):
            return

        self.ifc_import_settings.logger.info('Creating object {}'.format(element))

        if shape:
            # TODO: make names more meaningful
            mesh_name = f'mesh-{shape.geometry.id}'
            mesh = self.meshes.get(mesh_name)
            if mesh is None:
                # TODO: figure out a design pattern for native objects
                if element.is_a('IfcReinforcingBar'):
                    mesh = self.create_mesh(element, shape, is_curve=True)
                    mesh.bevel_object = self.native_data[element.GlobalId]['bevel_object']
                    mesh.use_fill_caps = True
                else:
                    mesh = self.create_mesh(element, shape)
                self.meshes[mesh_name] = mesh
        else:
            mesh = None

        obj = bpy.data.objects.new(self.get_name(element), mesh)

        if shape:
            m = shape.transformation.matrix.data
            mat = mathutils.Matrix(([m[0], m[1], m[2], 0],
                                    [m[3], m[4], m[5], 0],
                                    [m[6], m[7], m[8], 0],
                                    [m[9], m[10], m[11], 1]))
            mat.transpose()
            obj.matrix_world = mat
            self.material_creator.create(element, obj, mesh)

        self.add_element_attributes(element, obj)
        self.add_element_document_relations(element, obj)
        self.add_defines_by_type_relation(element, obj)
        self.add_product_definitions(element, obj)
        self.added_data[element.GlobalId] = obj
        return obj

    def merge_by_class(self):
        merge_set = {}
        for obj in self.added_data.values():
            if '/' in obj.name:
                merge_set.setdefault(obj.name.split('/')[0], []).append(obj)
        self.merge_objects(merge_set)

    def merge_by_material(self):
        merge_set = {}
        for obj in self.added_data.values():
            if not obj.material_slots:
                merge_set.setdefault('no-material', []).append(obj)
            else:
                merge_set.setdefault(obj.material_slots[0].name, []).append(obj)
        self.merge_objects(merge_set)

    def merge_objects(self, merge_set):
        for ifc_class, objs in merge_set.items():
            context_override = {}
            context_override['object'] = context_override['active_object'] = objs[0]
            context_override['selected_objects'] = context_override['selected_editable_objects'] = objs
            bpy.ops.object.join(context_override)

    def clean_mesh(self):
        obj = None
        last_obj = None
        for obj in self.added_data.values():
            if obj.type == 'MESH':
                obj.select_set(True)
                last_obj = obj
        if not obj:
            return
        bpy.context.view_layer.objects.active = last_obj
        context_override = {}
        bpy.ops.object.editmode_toggle(context_override)
        bpy.ops.mesh.remove_doubles(context_override)
        bpy.ops.mesh.tris_convert_to_quads(context_override)
        bpy.ops.mesh.dissolve_limited(context_override)
        bpy.ops.mesh.normals_make_consistent(context_override)
        bpy.ops.object.editmode_toggle(context_override)

    def add_product_definitions(self, element, obj):
        if not hasattr(element, 'IsDefinedBy') or not element.IsDefinedBy:
            return
        for definition in element.IsDefinedBy:
            if not definition.is_a('IfcRelDefinesByProperties'):
                continue
            if definition.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                self.add_pset(definition.RelatingPropertyDefinition, obj)
            elif definition.RelatingPropertyDefinition.is_a('IfcElementQuantity'):
                self.add_qto(definition.RelatingPropertyDefinition, obj)

    def add_type_product_psets(self, element, obj):
        if not hasattr(element, 'HasPropertySets') or not element.HasPropertySets:
            return
        for definition in element.HasPropertySets:
            if definition.is_a('IfcPropertySet'):
                self.add_pset(definition, obj)

    def add_pset(self, pset, obj):
        new_pset = obj.BIMObjectProperties.override_psets.add()
        new_pset.name = pset.Name
        if new_pset.name in schema.ifc.psets:
            for prop_name in schema.ifc.psets[new_pset.name]['HasPropertyTemplates'].keys():
                prop = new_pset.properties.add()
                prop.name = prop_name
        # Invalid IFC, but some vendors like Solidworks do this so we accomodate it
        if not pset.HasProperties:
            return
        for prop in pset.HasProperties:
            if prop.is_a('IfcPropertySingleValue') and prop.NominalValue:
                index = new_pset.properties.find(prop.Name)
                if index >= 0:
                    new_pset.properties[index].string_value = str(prop.NominalValue.wrappedValue)
                else:
                    new_prop = new_pset.properties.add()
                    new_prop.name = prop.Name
                    new_prop.string_value = str(prop.NominalValue.wrappedValue)

    def add_qto(self, qto, obj):
        new_qto = obj.BIMObjectProperties.qtos.add()
        new_qto.name = qto.Name
        if new_qto.name in schema.ifc.qtos:
            for prop_name in schema.ifc.qtos[new_qto.name]['HasPropertyTemplates'].keys():
                prop = new_qto.properties.add()
                prop.name = prop_name
        for prop in qto.Quantities:
            if prop.is_a('IfcPhysicalSimpleQuantity'):
                value = getattr(prop, '{}Value'.format(prop.is_a()[len('IfcQuantity'):]))
                if not value:
                    continue
                index = new_qto.properties.find(prop.Name)
                if index >= 0:
                    new_qto.properties[index].string_value = str(value)
                else:
                    new_prop = new_qto.properties.add()
                    new_prop.name = prop.Name
                    new_prop.string_value = str(value)

    def add_defines_by_type_relation(self, element, obj):
        related_type = None
        if self.file.schema == 'IFC2X3':
            if not hasattr(element, 'IsDefinedBy') or not element.IsDefinedBy:
                return
            for relationship in element.IsDefinedBy:
                if relationship.is_a('IfcRelDefinesByType'):
                    related_type = relationship.RelatingType
                    break
        else:
            if not hasattr(element, 'IsTypedBy') or not element.IsTypedBy:
                return
            related_type = element.IsTypedBy[0].RelatingType
        if related_type:
            obj.BIMObjectProperties.relating_type = self.type_products[related_type.GlobalId]

    def load_diff(self):
        if not self.ifc_import_settings.diff_file:
            return
        with open(self.ifc_import_settings.diff_file, 'r') as file:
            self.diff = json.load(file)

    def load_file(self):
        self.ifc_import_settings.logger.info('loading file {}'.format(self.ifc_import_settings.input_file))
        extension = self.ifc_import_settings.input_file.split('.')[-1]
        if extension == 'ifczip':
            with tempfile.TemporaryDirectory() as unzipped_path:
                with zipfile.ZipFile(self.ifc_import_settings.input_file, 'r') as zip_ref:
                    zip_ref.extractall(unzipped_path)
                for filename in Path(unzipped_path).glob('**/*.ifc'):
                    self.file = ifcopenshell.open(filename)
                    break
        elif extension == 'ifc':
            self.file = ifcopenshell.open(self.ifc_import_settings.input_file)
        ifc.IfcStore.file = self.file

    def set_ifc_file(self):
        bpy.context.scene.BIMProperties.ifc_file = self.ifc_import_settings.input_file
        ifc.IfcStore.path = 'self.ifc_import_settings.input_file'

    def calculate_unit_scale(self):
        units = self.file.by_type('IfcUnitAssignment')[0]
        for unit in units.Units:
            if not hasattr(unit, 'UnitType') \
                or unit.UnitType != 'LENGTHUNIT':
                continue
            while unit.is_a('IfcConversionBasedUnit'):
                self.unit_scale *= unit.ConversionFactor.ValueComponent.wrappedValue
                unit = unit.ConversionFactor.UnitComponent
            if unit.is_a('IfcSIUnit'):
                self.unit_scale *= SIUnitHelper.get_prefix_multiplier(unit.Prefix)

    def create_project(self):
        self.project = { 'ifc': self.file.by_type('IfcProject')[0] }
        self.project['blender'] = bpy.data.collections.new('IfcProject/{}'.format(self.project['ifc'].Name))
        bpy.context.scene.collection.children.link(self.project['blender'])
        obj = self.create_product(self.project['ifc'])
        if obj:
                self.project['blender'].objects.link(obj)
                del self.added_data[self.project['ifc'].GlobalId]

    def create_spatial_hierarchy(self):
        if self.project['ifc'].IsDecomposedBy:
            for rel_aggregate in self.project['ifc'].IsDecomposedBy:
                self.add_related_objects(self.project['blender'], rel_aggregate.RelatedObjects)

    def add_related_objects(self, parent, related_objects):
        for element in related_objects:
            if element.is_a('IfcSpace'):
                continue
            global_id = element.GlobalId
            collection = bpy.data.collections.new(self.get_name(element))
            self.spatial_structure_elements[global_id] = { 'blender': collection }
            parent.children.link(collection)
            obj = self.create_product(element)
            if obj:
                collection.objects.link(obj)
                del self.added_data[element.GlobalId]
            if element.IsDecomposedBy:
                for rel_aggregate in element.IsDecomposedBy:
                    self.add_related_objects(collection, rel_aggregate.RelatedObjects)

    def create_aggregates(self):
        rel_aggregates = [a for a in self.file.by_type('IfcRelAggregates')
                if a.RelatingObject.is_a('IfcElement')]
        for rel_aggregate in rel_aggregates:
            self.create_aggregate(rel_aggregate)

    def create_aggregate(self, rel_aggregate):
        collection = bpy.data.collections.new(f'IfcRelAggregates/{rel_aggregate.id()}')
        bpy.context.scene.collection.children.link(collection)
        bpy.context.view_layer.layer_collection.children[collection.name].hide_viewport = True

        instance = bpy.data.objects.new('{}/{}'.format(
            rel_aggregate.RelatingObject.is_a(),
            rel_aggregate.RelatingObject.Name),
            None)
        instance.instance_type = 'COLLECTION'
        instance.instance_collection = collection
        self.place_object_in_spatial_tree(rel_aggregate.RelatingObject, instance)

    def create_openings_collection(self):
        collection = bpy.data.collections.new('IfcOpeningElements')
        bpy.context.scene.collection.children.link(collection)

    def get_name(self, element):
        return '{}/{}'.format(element.is_a(), element.Name)

    def purge_diff(self):
        if not self.diff:
            return
        objects_to_purge = []
        for obj in bpy.data.objects:
            if 'GlobalId' not in obj.BIMObjectProperties.attributes:
                continue
            global_id = obj.BIMObjectProperties.attributes['GlobalId'].string_value
            if global_id in self.diff['deleted'] \
                or global_id in self.diff['changed'].keys():
                objects_to_purge.append(obj)
        bpy.ops.object.delete({'selected_objects': objects_to_purge})

    def create_product_legacy(self, element):
        if self.diff \
                and element.GlobalId not in self.diff['added'] \
                and element.GlobalId not in self.diff['changed'].keys():
            return

        self.ifc_import_settings.logger.info('Creating object {}'.format(element))
        self.time = time.time()
        if element.is_a('IfcOpeningElement'):
            return

        try:
            representation_id = self.get_representation_id(element)

            mesh_name = 'mesh-{}'.format(representation_id)
            mesh = self.meshes.get(mesh_name)
            if mesh is None \
                or representation_id is None:
                shape = ifcopenshell.geom.create_shape(self.settings, element)
                self.ifc_import_settings.logger.info('Shape was generated in {:.2f}'.format(time.time() - self.time))
                self.time = time.time()

                mesh = self.create_mesh(element, shape)
                self.meshes[mesh_name] = mesh
                self.mesh_shapes[mesh_name] = shape
            else:
                self.ifc_import_settings.logger.info('Mesh reused.')
        except:
            self.ifc_import_settings.logger.error('Failed to generate shape for {}'.format(element))
            return

        obj = bpy.data.objects.new(self.get_name(element), mesh)
        self.material_creator.create(element, obj, mesh)
        obj.matrix_world = self.get_element_matrix(element, mesh_name)
        self.add_element_attributes(element, obj)
        self.add_element_document_relations(element, obj)
        self.add_defines_by_type_relation(element, obj)
        self.add_product_definitions(element, obj)
        self.added_data[element.GlobalId] = obj

    def add_element_document_relations(self, element, obj):
        for association in element.HasAssociations:
            if association.is_a('IfcRelAssociatesDocument'):
                document_reference = association.RelatingDocument
                document = obj.BIMObjectProperties.documents.add()
                document.file = document_reference.Location

    def place_objects_in_spatial_tree(self):
        for global_id, obj in self.added_data.items():
            self.place_object_in_spatial_tree(self.file.by_guid(global_id), obj)

    def place_object_in_spatial_tree(self, element, obj):
        if hasattr(element, 'ContainedInStructure') \
                and element.ContainedInStructure \
                and element.ContainedInStructure[0].RelatingStructure:
            container = element.ContainedInStructure[0].RelatingStructure
            if container.is_a('IfcSpace'):
                if container.GlobalId in self.added_data:
                    obj.BIMObjectProperties.relating_structure = self.added_data[container.GlobalId]
                return self.place_object_in_spatial_tree(container, obj)
            relating_structure_global_id = container.GlobalId
            if relating_structure_global_id in self.spatial_structure_elements:
                self.spatial_structure_elements[relating_structure_global_id]['blender'].objects.link(obj)
        elif hasattr(element, 'Decomposes') \
                and element.Decomposes:
            collection = None
            if element.Decomposes[0].RelatingObject.is_a('IfcProject'):
                collection = bpy.data.collections.get(f'IfcProject/{element.Decomposes[0].RelatingObject.Name}')
            elif element.Decomposes[0].RelatingObject.is_a('IfcSpatialStructureElement'):
                global_id = element.Decomposes[0].RelatingObject.GlobalId
                if global_id in self.spatial_structure_elements:
                    collection = self.spatial_structure_elements[global_id]['blender']
            elif self.ifc_import_settings.should_import_aggregates:
                collection = bpy.data.collections.get(f'IfcRelAggregates/{element.Decomposes[0].id()}')
            else:
                return self.place_object_in_spatial_tree(element.Decomposes[0].RelatingObject, obj)
            if collection:
                collection.objects.link(obj)
            else:
                self.ifc_import_settings.logger.error('An element could not be placed in the spatial tree {}'.format(element))
        elif hasattr(element, 'HasFillings') \
                and element.HasFillings:
            bpy.data.collections.get('IfcOpeningElements').objects.link(obj)
        else:
            self.ifc_import_settings.logger.warning('Warning: this object is outside the spatial hierarchy')
            bpy.context.scene.collection.objects.link(obj)

    def add_element_attributes(self, element, obj):
        attributes = element.get_info()
        if element.is_a() in schema.ifc.elements:
            applicable_attributes = [a['name'] for a in schema.ifc.elements[element.is_a()]['attributes']]
            for key, value in attributes.items():
                if key not in applicable_attributes \
                        or value is None:
                    continue
                attribute = obj.BIMObjectProperties.attributes.add()
                attribute.name = key
                attribute.data_type = 'string'
                attribute.string_value = str(value)

    def get_element_matrix(self, element, mesh_name):
        element_matrix = self.get_local_placement(element.ObjectPlacement)

        # Blender supports reusing a mesh with a different transformation
        # applied at the object level. In contrast, IFC supports reusing a mesh
        # with a different transformation applied at the mesh level _as well as_
        # the object level. For this reason, if the end-goal is to re-use mesh
        # data, we must combine IFC's mesh-level transformation into Blender's
        # object level transformation.

        # The first step to do this is to _undo_ the mesh-level transformation
        # from whatever shared mesh we are using, as it is not necessarily the
        # same as the current mesh.
        shared_shape_transformation = self.get_representation_cartesian_transformation(
            self.file.by_id(self.mesh_shapes[mesh_name].product.id()))
        if shared_shape_transformation:
            shared_transform = self.get_cartesiantransformationoperator(shared_shape_transformation)
            shared_transform.invert()
            element_matrix = element_matrix @ shared_transform

        # The next step is to apply the current element's mesh level
        # transformation to our current element's object transformation
        transformation = self.get_representation_cartesian_transformation(element)
        if transformation:
            element_matrix = self.get_cartesiantransformationoperator(transformation) @ element_matrix

        element_matrix[0][3] *= self.unit_scale
        element_matrix[1][3] *= self.unit_scale
        element_matrix[2][3] *= self.unit_scale

        return element_matrix

    def get_representation_id(self, element):
        if not element.Representation:
            return None
        for representation in element.Representation.Representations:
            if not representation.is_a('IfcShapeRepresentation'):
                continue
            if representation.RepresentationIdentifier == 'Body' \
                and representation.RepresentationType != 'MappedRepresentation':
                return representation.id()
            elif representation.RepresentationIdentifier == 'Body':
                return representation.Items[0].MappingSource.MappedRepresentation.id()

    def get_representation_cartesian_transformation(self, element):
        if not element.Representation:
            return None
        for representation in element.Representation.Representations:
            if not representation.is_a('IfcShapeRepresentation'):
                continue
            if representation.RepresentationIdentifier == 'Body' \
                and representation.RepresentationType == 'MappedRepresentation':
                return representation.Items[0].MappingTarget

    def create_mesh(self, element, shape, is_curve=False):
        try:
            if hasattr(shape, 'geometry'):
                geometry = shape.geometry
            else:
                geometry = shape

            if is_curve:
                return self.create_curve(geometry)

            mesh = bpy.data.meshes.new(geometry.id)
            f = geometry.faces
            e = geometry.edges
            v = geometry.verts
            vertices = [[v[i], v[i + 1], v[i + 2]]
                     for i in range(0, len(v), 3)]
            faces = [[f[i], f[i + 1], f[i + 2]]
                     for i in range(0, len(f), 3)]
            if faces:
                edges = []
            else:
                edges = [[e[i], e[i + 1]]
                         for i in range(0, len(e), 2)]
            mesh.from_pydata(vertices, edges, faces)
            ios_materials = []
            for mat in geometry.materials:
                if mat.original_name():
                    ios_materials.append(mat.original_name())
                else:
                    ios_materials.append(mat.name)
            mesh['ios_materials'] = ios_materials
            mesh['ios_material_ids'] = geometry.material_ids
            return mesh
        except:
            self.ifc_import_settings.logger.error('Could not create mesh for {}: {}/{}'.format(element.GlobalId, self.get_name(element)))

    def create_curve(self, geometry):
        curve = bpy.data.curves.new(geometry.id, type='CURVE')
        curve.dimensions = '3D'
        curve.resolution_u = 2
        polyline = curve.splines.new('POLY')
        e = geometry.edges
        v = geometry.verts
        vertices = [[v[i], v[i + 1], v[i + 2], 1]
                 for i in range(0, len(v), 3)]
        edges = [[e[i], e[i + 1]]
                 for i in range(0, len(e), 2)]
        v2 = None
        for edge in edges:
            v1 = vertices[edge[0]]
            if v1 != v2:
                polyline = curve.splines.new('POLY')
                polyline.points[-1].co = v1
            v2 = vertices[edge[1]]
            polyline.points.add(1)
            polyline.points[-1].co = v2
        return curve

    def a2p(self, o, z, x):
        y = z.cross(x)
        r = mathutils.Matrix((x, y, z, o))
        r.resize_4x4()
        r.transpose()
        return r

    def get_axis2placement(self, plc):
        z = mathutils.Vector(plc.Axis.DirectionRatios if plc.Axis else (0,0,1))
        x = mathutils.Vector(plc.RefDirection.DirectionRatios if plc.RefDirection else (1,0,0))
        o = plc.Location.Coordinates
        return self.a2p(o,z,x)

    def get_cartesiantransformationoperator(self, plc):
        x = mathutils.Vector(plc.Axis1.DirectionRatios if plc.Axis1 else (1,0,0))
        z = x.cross(mathutils.Vector(plc.Axis2.DirectionRatios if plc.Axis2 else (0,1,0)))
        o = plc.LocalOrigin.Coordinates
        return self.a2p(o,z,x)

    def get_local_placement(self, plc):
        if plc.PlacementRelTo is None:
            parent = mathutils.Matrix()
        else:
            parent = self.get_local_placement(plc.PlacementRelTo)
        return parent @ self.get_axis2placement(plc.RelativePlacement)

class IfcImportSettings:
    def __init__(self):
        self.logger = None
        self.input_file = None
        self.should_auto_set_workarounds = True
        self.should_ignore_site_coordinates = False
        self.should_ignore_building_coordinates = False
        self.should_reset_absolute_coordinates = False
        self.should_import_curves = False
        self.should_import_opening_elements = False
        self.should_import_spaces = False
        self.should_treat_styled_item_as_material = False
        self.should_use_cpu_multiprocessing = False
        self.should_use_legacy = False
        self.should_merge_by_class = False
        self.should_merge_by_material = False
        self.should_import_aggregates = True
        self.should_clean_mesh = True
        self.diff_file = None