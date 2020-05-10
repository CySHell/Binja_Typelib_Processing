from clang.cindex import *
from .Libraries.ws2_32 import ws2_32 as ws2
import binaryninja as bn
from .Libraries.ntdll import ntdll_dll as ntdll

from . import ast_handlers


def pre_define_types(bv: bn.BinaryView, library):
    for var_type, var_name in library.pre_load_definition.items():
        t, n = bv.parse_type_string(f'{var_type} {var_name}')
        bv.define_user_type(n, t)

    for forward_decl_struct in library.forward_declarations['struct']:
        struct = bn.Structure()
        bv.define_user_type(forward_decl_struct, bn.Type.structure_type(bn.Structure()))
    for forward_decl_typedef in library.forward_declarations['typedef']:
        print(forward_decl_typedef)
        var_type, var_name = bv.parse_type_string(f'{forward_decl_typedef};')
        bv.define_user_type(var_name, var_type)


def pp(bv: bn.BinaryView):
    pre_define_types(bv, ntdll)

    Config.set_library_file('C:\\Program Files\\LLVM\\bin\\libclang.dll')
    index: Index = Index.create()
    tu: TranslationUnit = index.parse(ntdll.header_list[0], args=ntdll.pre_proccessor_args)  # args for clang parser

    root_node = tu.cursor

    for node in root_node.get_children():
        bn.log.log_debug(f'{"*" * 30}\nDEFINING NODE: \n {node.spelling} {node.type.spelling} \n'
                         f'node.kind: {node.kind}, node.type.kind: {node.type.kind}\n {"*" * 30}')
        ast_handlers.define_type(node, bv)


    ####################################################################
    ntdll_tl = bn.TypeLibrary.new(bn.Architecture["x86"], "ntdll.dll")
    ntdll_tl.add_platform(bn.Platform["windows-x86"])

    for node in root_node.get_children():
        bn.log.log_debug(f'{"*" * 30}\nEXPORTING NODE: \n {node.spelling} {node.type.spelling} \n'
                         f'node.kind: {node.kind}, node.type.kind: {node.type.kind}\n {"*" * 30}')
        var_type = bv.get_type_by_name(node.spelling)
        if isinstance(var_type, bn.Type):
            bv.export_type_to_library(ntdll_tl, node.spelling, var_type)
    ntdll_tl.finalize()
    ntdll_tl.write_to_file('C:\\Users\\rowr1\\OneDrive\\Header-Files\\BinaryNinja type libraries\\ntdll_type_lib.btl')
    ###################################################################
