bl_info = {
    "name": "IfcBlenderExport",
    "description": "Export files in the "
        "Industry Foundation Classes (.ifc) file format",
    "author": "Dion Moult, IfcOpenShell",
    "blender": (2, 80, 0),
    "location": "File > Export",
    "tracker_url": "https://sourceforge.net/p/ifcopenshell/"
        "_list/tickets?source=navbar",
    "category": "Import-Export"
    }

import bpy
from . import ui
from . import operator

classes = (
    operator.AssignClass,
    operator.SelectDataDir,
    operator.SelectSchemaDir,
    operator.ExportIFC,
    ui.BIMProperties,
    ui.BIMPanel,
    )

def menu_func(self, context):
    self.layout.operator(operator.ExportIFC.bl_idname,
         text="Industry Foundation Classes (.ifc)")

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_export.append(menu_func)
    bpy.types.Scene.BIMProperties = bpy.props.PointerProperty(type=ui.BIMProperties)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func)
    del(bpy.types.Scene.BIMProperties)

if __name__ == "__main__":
    register()