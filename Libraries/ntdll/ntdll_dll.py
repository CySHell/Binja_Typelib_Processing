# ntdll.dll
from ...directories_config import *

header_list = [base_libraries_folder + 'ntdll\\ntdll.h']

define_list = ['-D _M_AMD64', '-D _M_X64']

target_arch = '--target=x86_64-pc-windows-msvc'

pre_proccessor_args = ['-fms-compatibility', '-fms-extensions', '-fmsc-version=1300', '-o=ntdll.h']
pre_proccessor_args.extend(define_list)
pre_proccessor_args.append(target_arch)

# These are types that are used in the header but not explicitly defined.
pre_load_definition = {
    'bool': '_Bool'
}

# These are forward declarations that mainly involve cross structure recursive definition (struct A has an element
# of a struct B, which is not yet defined, and class B has an element of class A.
# This type of case is very hard to find using the current libclang API, so we just manually enter it here to be forward
# declared.
forward_declarations = {'struct': ('_RTL_CRITICAL_SECTION', '_RTL_CRITICAL_SECTION_DEBUG', 'IRpcChannelBuffer'),
                        'typedef': ('_RTL_CRITICAL_SECTION_DEBUG* PRTL_CRITICAL_SECTION_DEBUG',)}
