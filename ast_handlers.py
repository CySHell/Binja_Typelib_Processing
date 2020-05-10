import binaryninja as bn
from clang.cindex import *
from typing import *
import xxhash

# Keys are the original spelling of the type\object name in the header file, the Value is the name entered into
# the binaryView.
# BinaryNinja doesn't accept certain user defined names so we must alter them (e.g ptrdiff_t).
processed_types = dict()

# This is a list of libclangs' base types (mainly used in the check_if_base_type() function
base_types = [TypeKind.BOOL, TypeKind.CHAR16, TypeKind.CHAR32, TypeKind.CHAR_S,
              TypeKind.CHAR_U, TypeKind.DOUBLE, TypeKind.FLOAT, TypeKind.FLOAT128,
              TypeKind.HALF, TypeKind.INT, TypeKind.UINT, TypeKind.INT128, TypeKind.LONG,
              TypeKind.LONGLONG, TypeKind.LONGDOUBLE, TypeKind.SCHAR, TypeKind.SHORT,
              TypeKind.ULONG, TypeKind.UCHAR, TypeKind.ULONGLONG, TypeKind.USHORT,
              TypeKind.VOID, TypeKind.WCHAR]

# This is a list of compiler directives to remove from the type string, since binaryNinja can't handle them.
compiler_directives = ['__unaligned', '__attribute__((stdcall))']

# Incomplete arrays have no size, so we declare an arbitrary size in order to be able to parse them.
INCOMPLETE_ARRAY_ARBITRARY_SIZE = 0x1000

# Binary Ninja cannot parse these types, so we need to change them to simply 'void'
void_types = ('const void', 'const volatile void', 'volatile void', '__unaligned void', 'const __unaligned void')


def define_type(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'define_type: Dispatch for "{node.type.spelling} {node.spelling}", CursorKind: {node.kind}, type '
                     f'{node.type.spelling}, TypeKind: {node.type.kind}')
    # Dispatch the correct handler for the declaration recursively.
    # It is important to check for type kind before we check for cursor kind in order
    # to detect arrays and such.
    if node.spelling:
        # For some reason libclang parses some typedefs (usually ENUM_DECL) as having no spelling, but doesn't
        # recognize them as anonymous.
        # BinaryNinja returns a type for the empty string ('') - which causes problems when trying to determine if
        # the type is already defined.
        current_type = bv.get_type_by_name(node.type.spelling)
    else:
        current_type = None
    if isinstance(current_type, bn.types.Type):
        # Check if type already defined.
        bn.log.log_debug(f'define_type: type {node.spelling} already defined, skipping re-definition.')
        var_type = current_type
        var_name = node.spelling
        return var_name, var_type
    elif check_if_base_type(node.type):
        var_type, var_name = bv.parse_type_string(f'{node.type.spelling} {node.spelling}')
        return str(var_name), var_type
    elif node.is_anonymous():
        return define_anonymous_type(node, bv)
    elif node.type.kind == TypeKind.ELABORATED:
        return define_type(node.type.get_declaration(), bv)
    elif node.type.kind == TypeKind.CONSTANTARRAY:
        return constantarray_type(node, bv)
    elif node.type.kind == TypeKind.INCOMPLETEARRAY:
        return incompletearray_type(node, bv)
    elif node.type.kind == TypeKind.FUNCTIONPROTO:
        return functionproto_type(node, bv)
    elif node.type.kind == TypeKind.POINTER:
        return pointer_type(node, bv)
    elif node.kind == CursorKind.TYPEDEF_DECL:
        if node.type.kind == TypeKind.TYPEDEF:
            if node.underlying_typedef_type.kind == TypeKind.FUNCTIONPROTO:
                return function_decl(node, bv)
            elif node.underlying_typedef_type.kind == TypeKind.POINTER:
                return pointer_type(node, bv)
        return typedef_decl(node, bv)
    elif node.kind == CursorKind.PARM_DECL:
        if node.type.kind == TypeKind.TYPEDEF:
            return typedef_decl(node, bv)
        else:
            bn.log.log_debug(f'define_type: Unhandled case - node.kind {node.kind}, node.type.kind {node.type.kind}')
    elif node.kind == CursorKind.VAR_DECL:
        return var_decl(node, bv)
    elif node.kind == CursorKind.FUNCTION_DECL:
        return function_decl(node, bv)
    elif node.kind == CursorKind.ENUM_DECL:
        return enum_decl(node, bv)
    elif node.kind == CursorKind.STRUCT_DECL:
        return struct_decl(node, bv)
    elif node.kind == CursorKind.FIELD_DECL:
        return field_decl(node, bv)
    elif node.kind == CursorKind.UNION_DECL:
        return struct_decl(node, bv)
    else:
        bn.log.log_info(f'no handler for cursorKind {node.kind}')


def pointer_type(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'pointer_type: {node.type.spelling} {node.spelling}, \n'
                     f'node.type.kind: {node.type.kind} \n')
    if node.type.kind == TypeKind.TYPEDEF:
        pointee_type = node.underlying_typedef_type.get_pointee()
    elif node.type.kind == TypeKind.POINTER:
        pointee_type = node.type.get_pointee()
    else:
        bn.log.log_debug(f'pointer_type: Unhandled node type: {node.type.kind}')
        return

    if check_if_base_type(pointee_type):
        pointee_type_spelling = pointee_type.spelling
        if pointee_type_spelling in void_types:
            # BinaryNinja can't parse the expression 'const void'.
            pointee_type_spelling = 'void'
        # If its a base type then no need to define pointee type.
        bn.log.log_debug(f'pointer_type: Parsing type string: {pointee_type_spelling}')
        bn_pointee_type, name = bv.parse_type_string(pointee_type_spelling)
        pointer = bn.Type.pointer(bv.arch, bn_pointee_type)
    else:
        pointee_node = pointee_type.get_declaration()
        if pointee_node.kind == CursorKind.NO_DECL_FOUND:
            # Some types of TypeKind.TYPEDEF have no declaration node because they the type is just a pointer.
            # example: typedef EXCEPTION_ROUTINE *PEXCEPTION_ROUTINE;
            bn.log.log_debug(f'pointer_type: No declaration found for: {pointee_type.spelling} \n'
                             f'                                        pointee_type.kind: {pointee_type.kind}')
            if pointee_type.kind == TypeKind.FUNCTIONPROTO:
                # A special case happens when a type is a typedef for a function pointer - the function might be
                # an anonymous function that was not previously defined, so we must define it first (can't just parse
                # the string  with parse_type_string().
                # Example: typedef void
                #               (__stdcall *PIMAGE_TLS_CALLBACK) (
                #                                                   PVOID DllHandle,
                #                                                   DWORD Reason,
                #                                                   PVOID Reserved
                #                                                 );
                bn_pointee_name, bn_pointee_type = function_decl(node, bv)
                pointer = bn.Type.pointer(bv.arch, bn_pointee_type)
            elif pointee_type.kind == TypeKind.FUNCTIONNOPROTO:
                # FUNCTIONNOPROTO means there are no arguments, only a possible return type
                pointee_result_type = pointee_type.get_result()
                if check_if_base_type(pointee_result_type):
                    # Result is a base type, thus no declaration node.
                    # Example: long ()
                    pointee_result_string = pointee_result_type.spelling
                    if pointee_result_string in void_types:
                        pointee_result_string = 'void'
                    bn_result_type, bn_result_name = bv.parse_type_string(pointee_result_string)
                else:
                    result_type = pointee_type.get_result().get_declaration()
                    bn_result_name, bn_result_type = define_type(result_type, bv)
                pointer = bn.Type.pointer(bv.arch, bn.Type.function(bn_result_type, []))
            elif pointee_type.kind == TypeKind.POINTER:
                # we are dealing with a pointer to a pointer
                if check_if_base_type(pointee_type.get_pointee()):
                    type_string = pointee_type.get_pointee().spelling
                    if type_string in void_types:
                        type_string = 'void'
                    bn_pointee_type, bn_pointee_name = bv.parse_type_string(type_string)
                elif pointee_type.get_pointee().kind == TypeKind.POINTER:
                    # We have multiple nested pointers.
                    # Example: int ****a;
                    # The problem here is that if the pointee type is also a pointer, then it has no declaration node,
                    # so we can't call pointer_type() on it directly.
                    nested_pointer_count = 1
                    current_pointer_type = pointee_type.get_pointee()
                    while current_pointer_type.kind == TypeKind.POINTER:
                        nested_pointer_count += 1
                        current_pointer_type = current_pointer_type.get_pointee()
                    if check_if_base_type(current_pointer_type):
                        bn_pointee_type, bn_pointee_name = bv.parse_type_string(current_pointer_type.spelling)
                    else:
                        bn_pointee_name, bn_pointee_type = define_type(current_pointer_type, bv)
                    temp_bn_pointer_type = bn.Type.pointer(bv.arch, bn_pointee_type)
                    for nesting_level in range(nested_pointer_count):
                        temp_bn_pointer_type = bn.Type.pointer(bv.arch, temp_bn_pointer_type)
                    bn_pointee_type = bn.Type.pointer(bv.arch, temp_bn_pointer_type)
                elif pointee_type.get_pointee().get_declaration().kind == CursorKind.NO_DECL_FOUND:
                    # For some reason there is no declaration of the pointee.
                    # Manually parse the type and hope it was previously defined.
                    # TODO: Find a way to handle a case where the type was not already defined.
                    print(f'pointee_type.get_pointee().get_named_type().kind: {pointee_type.get_pointee().get_named_type().kind}')
                    # The reason I am parsing the pointee_type and not pointee_type.get_pointee() is that in some
                    # cases the pointer is pointing to a function prototype that has no declaration, and it is much
                    # easier to just parse the pointer to a known type then parse the underlying type.
                    bn_pointee_type, bn_pointee_name = bv.parse_type_string(pointee_type.spelling)
                else:
                    bn_pointee_name, bn_pointee_type = define_type(pointee_type.get_pointee().get_declaration(), bv)
                pointer = bn.Type.pointer(bv.arch, bn_pointee_type)
            else:
                bn_pointee_type, bn_pointee_name = bv.parse_type_string(node.underlying_typedef_type.spelling)
                pointer = bn.Type.pointer(bv.arch, bn_pointee_type)
        else:
            bn_pointee_type = bv.get_type_by_name(pointee_node.spelling)
            if bn_pointee_type is None:
                # need to define the pointee type before declaring the pointer
                bn_pointee_name, bn_pointee_type = define_type(pointee_node, bv)
                pointer = bn.Type.pointer(bv.arch, bn_pointee_type)
            else:
                # type already defined in the binaryView.
                pointer = bn.Type.pointer(bv.arch, bn_pointee_type)

    bv.define_user_type(node.spelling, pointer)
    bn.log.log_debug(f'pointer_type: Successfully defined : {node.spelling}')
    return node.spelling, pointer


def functionproto_type(node: Cursor, bv: bn.BinaryView):
    # A libclang node with a TypeKind FUNCTIONPROTO is exactly the same as a libclang node with a CursorKind FUNCTION
    if node.kind == CursorKind.TYPEDEF_DECL or node.kind == CursorKind.PARM_DECL or not node.is_definition():
        bn.log.log_debug(f'functionproto_type: Processing  {node.spelling}')
        return function_decl(node, bv)
    else:
        # If the CursorKind is not TYPEDEF_DECL or PARM_DECL but it is a definition - it means the header file contains
        # the actual implementation of the function - we do not want to parse such functions.
        bn.log.log_debug(f'functionproto_type: {node.spelling} contains full function implementation. skipping.')
        pass


def constantarray_type(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'constantarray_type: {node.type.spelling} {node.spelling} \n'
                     f'                    node.kind: {node.kind}, node.type.kind: {node.type.kind}')
    element_type = node.type.get_array_element_type()
    bn.log.log_debug(f'constantarray_type: element_type: {element_type.spelling} \n'
                     f'                    element_type.kind: {element_type.kind}')

    array = None
    element_type_node = None

    bn_element_type = bv.get_type_by_name(element_type.spelling)
    if bn_element_type:
        # element type is already defined in the binaryView
        array = bn.Type.array(bn_element_type, node.type.get_array_size())
        bn.log.log_debug(f'constantarray_type: {element_type.spelling} already defined in the binaryView.')
    elif node.type.get_array_element_type().get_declaration().is_anonymous():
        # Anonymous struct\union\enum as the array member type
        element_type_node = node.type.get_array_element_type().get_declaration()
        anonymous_name, bn_anonymous_type = define_anonymous_type(element_type_node, bv)
        array = bn.Type.array(bn_anonymous_type, node.type.get_array_size())
        bn.log.log_debug(f'constantarray_type: Successfully proccessed anonymous type: {bn_anonymous_type} .')
    else:
        if check_if_base_type(element_type):
            # If its a base type then it wont apear in bv.get_type_by_name() but it is still defined.
            var_type, name = bv.parse_type_string(element_type.spelling)
            array = bn.Type.array(var_type, node.type.get_array_size())
        else:
            # Not a libclang base type, need to define it normally in the binaryView.
            if node.type.get_array_element_type().kind == TypeKind.POINTER:
                # The element is a pointer, so it won't have a declaration.
                # Get the declaration of the pointed type and create a binaryNinja pointer object as the type.
                if check_if_base_type(node.type.get_array_element_type().get_pointee()):
                    # The pointed type is a base type, parse it directly.
                    bn_element_type, bn_element_name = bv.parse_type_string(
                        node.type.get_array_element_type().get_pointee().spelling
                    )
                    pointer = bn.Type.pointer(bv.arch, bn_element_type)
                    array = bn.Type.array(pointer, node.type.get_array_size())
                else:
                    element_type_node = node.type.get_array_element_type().get_pointee().get_declaration()
            elif node.type.get_array_element_type().kind == TypeKind.CONSTANTARRAY:
                # The element type is another constant array, meaning we are dealing with a matrix.
                # Example: int a[3][4][5]
                if check_if_base_type(node.type.get_array_element_type().get_array_element_type()):
                    # The underlying matrix type is a base type, parse it directly.
                    bn_element_type, bn_element_name = bv.parse_type_string(
                        node.type.get_array_element_type().get_array_element_type().spelling
                    )
                    temp_array = bn.Type.array(bn_element_type, node.type.get_array_element_type().get_array_size())
                    array = bn.Type.array(temp_array, node.type.get_array_size())
                else:
                    element_type_node = node.type.get_array_element_type().get_array_element_type().get_declaration()
            else:
                element_type_node = node.type.get_array_element_type().get_declaration()

            if not array:
                # If array is defined at this point it means we have an array of pointers or a matrix, in which case
                # it was already handled and defined above.
                bn_element_name, bn_element_type = define_type(element_type_node, bv)
                array = bn.Type.array(bn_element_type, node.type.get_array_size())
    bv.define_user_type(node.spelling, array)
    bn.log.log_debug(f'constantarray_type: Successfully defined: {node.type.spelling} {node.spelling}')
    return node.spelling, array


def incompletearray_type(node: Cursor, bv: bn.BinaryView):
    # TODO: There is no good way to parse an incomplete array into binaryNinja since we do not know its size.
    # For now, convert an incomplete array to a complete array with a very big size since it will probably be defined
    # on the heap anyway.
    bn.log.log_debug(f'incompletearray_type: Processing {node.type.spelling} {node.spelling}, \n'
                     f'node.kind: {node.kind}, node.type.kind: {node.type.kind}')
    bn_array_element_type = node.type.get_array_element_type()
    if check_if_base_type(bn_array_element_type):
        var_type, var_name = bv.parse_type_string(bn_array_element_type.spelling)
    elif bn_array_element_type.kind == TypeKind.POINTER:
        # The array element type is a pointer - it does not have a declaration node so we cannot directly call
        # define_type().
        # Example: int *a[]
        if check_if_base_type(bn_array_element_type.get_pointee()):
            pointee_type_string = bn_array_element_type.get_pointee().spelling
            if pointee_type_string in void_types:
                pointee_type_string = 'void'
            pointee_var_type, pointee_var_name = bv.parse_type_string(pointee_type_string)
        else:
            pointee_var_name, pointee_var_type = define_type(bn_array_element_type.get_pointee().get_declaration(), bv)
        var_type = bn.Type.pointer(bv.arch, pointee_var_type)
    else:
        var_name, var_type = define_type(bn_array_element_type.get_declaration(), bv)
    array = bn.Type.array(var_type, INCOMPLETE_ARRAY_ARBITRARY_SIZE)

    return node.spelling, array


def check_if_base_type(type: Type):
    # In libclang, a base type is a type that has no declaration since it is a baes
    # type of the c language.
    # Examples of base types in libclang: Typekind.UCHAR, Typekind.INT etc
    if type.kind in base_types:
        bn.log.log_debug(f'check_if_base_type: {type.spelling} is a base type.')
        return True
    else:
        return False


def typedef_decl(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'typedef_decl: {node.underlying_typedef_type.spelling} {node.spelling}, \n'
                     f'underlying_typedef_type: {node.underlying_typedef_type.kind}')
    if node.spelling and bv.get_type_by_name(node.spelling):
        bn.log.log_debug(f'typedef_decl: Type already defined')
        return node.spelling, bv.get_type_by_name(node.spelling)
    elif not node.underlying_typedef_type.spelling:
        try:
            var_type, name = bv.parse_type_string(f'{node.type.spelling} {node.spelling}')
        except Exception as e:
            bn.log.log_debug(f'typedef_decl: Failed to parse {node.type.spelling} {node.spelling}, with exception {e}')
    else:

        # Sanitize the type - remove any compiler directives such as __aligned and such.
        underlying_typedef_type_string = remove_compiler_directives(node.underlying_typedef_type.spelling)
        try:
            var_type, name = bv.parse_type_string(f'{underlying_typedef_type_string}')
            # The reason we are not using the name inside the parsed string is that sometimes you get a typedef
            # like 'int [1] td', and if you parse it like that it's a binaryNinja exception.
            # instead we parse 'int [1]' and attach the name of the typedef to it afterwards.
            name = node.spelling
            bn.log.log_debug(f'typedef_decl: Successfully parsed {underlying_typedef_type_string} {node.spelling}')
        except SyntaxError as se:
            if 'syntax error' in str(se):
                if node.spelling.endswith('_t'):
                    # Some variables names are internal to binaryNinja and cannot be used. These var names usually
                    # end with _t, for example size_t \ ptrdiff_t etc.
                    # In order to not clash with the internal vars, change the _t to _T.
                    altered_spelling = node.spelling[:-1] + 'T'
                    var_type, name = bv.parse_type_string(f'{underlying_typedef_type_string} {altered_spelling}')
                elif 'is not defined' in str(se):
                    var_type, name = bv.define_user_type(underlying_typedef_type_string)
                else:
                    bn.log.log_debug(f'typedef_decl: Failed to parse {node.underlying_typedef_type.spelling} '
                                     f'{node.spelling}')

    try:
        bv.define_user_type(name, var_type)
        bn.log.log_debug(f'typedef_decl: Successfully processed {node.underlying_typedef_type.spelling} '
                         f'{node.spelling}')
        return str(name), var_type
    except Exception as e:
        bn.log.log_debug(f'typedef_decl: Failed Processing {node.underlying_typedef_type.spelling} '
                         f'{node.spelling} with exception {e}')


def remove_compiler_directives(type_str: str):
    sanitized_str = ''
    for str_token in type_str.split():
        if str_token in compiler_directives:
            continue
        sanitized_str += str_token + ' '
    return sanitized_str


def var_decl(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'var_decl: Processing var {node.underlying_typedef_type.spelling} {node.spelling}')
    var_type, name = bv.parse_type_string(f'{node.type.spelling} {node.spelling}')

    try:
        bv.define_user_type(name, var_type)
        bn.log.log_debug(f'var_decl: Successfully processed var {node.underlying_typedef_type.spelling} '
                         f'{node.spelling}')
        return str(name), var_type
    except Exception as e:
        bn.log.log_debug(f'var_decl: Failed Processing var {node.underlying_typedef_type.spelling} {node.spelling} '
                         f'with exception {e}')


def function_decl(node: Cursor, bv: bn.BinaryView):
    func_params: List = list()
    variable_arguments = False
    function_calling_convention: bn.CallingConvention = bv.platform.default_calling_convention

    bn.log.log_debug(f'function_decl: Processing function {node.spelling} \n'
                     f'               node.kind: {node.kind}, node.type.kind: {node.type.kind}')

    if node.kind == CursorKind.TYPEDEF_DECL:
        if node.type.kind == TypeKind.TYPEDEF:
            if node.underlying_typedef_type.kind == TypeKind.POINTER:
                # A special case in which we have a typedef for a function pointer to an anonymous function, so the
                # underlying type is a POINTER and not the actual function declaration. because it is an anonymous
                # function defined within a typedef there is no declaration node for it, only a type node.
                # Example: typedef void
                #                   (__stdcall *PIMAGE_TLS_CALLBACK) (
                #                                                       PVOID DllHandle,
                #                                                       DWORD Reason,
                #                                                       PVOID Reserved
                #                                                    );
                arg_types = node.underlying_typedef_type.get_pointee().argument_types()
                node_result_type = node.underlying_typedef_type.get_pointee().get_result()
                variable_arguments = node.underlying_typedef_type.get_pointee().is_function_variadic()
            else:
                arg_types = node.underlying_typedef_type.argument_types()
                node_result_type = node.underlying_typedef_type.get_result()
                variable_arguments = node.underlying_typedef_type.is_function_variadic()
        else:
            arg_types = node.type.argument_types()
            node_result_type = node.type.get_result()
            variable_arguments = node.type.is_function_variadic()
        # This is a libclang function prototype - need to use node.argument_types() to get all types.
        for param_type in arg_types:
            bn.log.log_debug(f'function_decl: Processing parameter type - {param_type.spelling} \n'
                             f'               param_type.kind: {param_type.kind}')
            if param_type.kind == TypeKind.INCOMPLETEARRAY:
                # An incomplete array cannot be parsed by binary ninja, need to manually create it.
                # This type usually has no declaration node, so cannot call define_type() on it.
                # Example: int a[]
                if check_if_base_type(param_type.get_array_element_type()):
                    arr_var_type, var_name = bv.parse_type_string(param_type.get_array_element_type().spelling)
                elif param_type.get_array_element_type().kind == TypeKind.POINTER:
                    # Example: int *a[]
                    # The pointer type has no declaration node so can't call define_type() directly.
                    pointee_name, pointee_type = define_type(
                        param_type.get_array_element_type().get_pointee().get_declaration(), bv
                    )
                    arr_var_type = bn.Type.pointer(bv.arch, pointee_type)
                    # TODO: Need to figure out a way to get the name of a parameter of this type.
                    var_name = ''
                else:
                    var_name, arr_var_type = define_type(param_type.get_array_element_type().get_declaration(), bv)
                var_type = bn.Type.array(arr_var_type, INCOMPLETE_ARRAY_ARBITRARY_SIZE)
            else:
                var_type, var_name = bv.parse_type_string(f'{remove_compiler_directives(param_type.spelling)}')
            p = bn.FunctionParameter(var_type, str(var_name))
            func_params.append(p)
    elif node.type.kind == TypeKind.POINTER:
        # If we got here, it means the pointee type is a FUNCTIONPROTO but has no declaration (if it had a declaration
        # then node arguemnt would be the declaration node itself and not a pointer.
        # Example: typedef struct _NCB {
        #                               UCHAR ncb_command;
        #                               void (__stdcall *ncb_post)( struct _NCB * );
        #                               } NCB, *PNCB;
        # ncb_post is a pointer to a FUNCTIONPROTO that has no Cursor node, only a type node.
        arg_types = node.type.get_pointee().argument_types()
        node_result_type = node.type.get_pointee().get_result()
        variable_arguments = node.type.get_pointee().is_function_variadic()
        for param_type in arg_types:
            bn.log.log_debug(f'function_decl: Processing pointee parameter type - {param_type.spelling} \n'
                             f'                                  param_type.kind: {param_type.kind}')
            param_type_string = remove_compiler_directives(param_type.spelling)
            if param_type.kind == TypeKind.INCOMPLETEARRAY:
                # Special case where we have an incomplete array without a declaration node, so we can't use
                # define_type().
                # Example: const PROPSPEC []
                # Since we know the base type of the array is already defined, all we need to do is modify the string
                # slightly so that binaryNinja can parse it (binja parser doesn't accept an incomplete array, it must
                # have an array size.
                # TODO: Find a more elegant way to insert an array size to the string.
                param_type_string = param_type_string.replace('[]', f'[{INCOMPLETE_ARRAY_ARBITRARY_SIZE}]')
            var_type, var_name = bv.parse_type_string(param_type_string)
            p = bn.FunctionParameter(var_type, str(var_name))
            func_params.append(p)
    else:
        node_result_type = node.type.get_result()
        if node.type.kind == TypeKind.FUNCTIONNOPROTO:
            # FUNCTIONNOPROTO means there are no arguments, only a possible return type
            pass
        else:
            variable_arguments = node.type.is_function_variadic()
            for param in node.get_arguments():
                bn.log.log_debug(f'function_decl: Processing parameter - {param.type.spelling} {param.spelling} \n'
                                 f'               param.kind: {param.kind}, param.type.kind: {param.type.kind}')
                var_name, var_type = define_type(param, bv)
                p = bn.FunctionParameter(var_type, str(var_name))
                func_params.append(p)
                bn.log.log_debug(f'function_decl: Successfully Processed parameter - {param.type.spelling} '
                                 f'{param.spelling}')

    func_return_val_type, ret_name = bv.parse_type_string(remove_compiler_directives(node_result_type.spelling))

    # No direct way to get the calling convention specified in the source code, need to iterate tokens and find it
    for token in node.get_tokens():
        if token.kind == TokenKind.KEYWORD:
            if token.spelling == '__cdecl':
                function_calling_convention: bn.CallingConvention = bv.platform.cdecl_calling_convention
            elif token.spelling == '__fastcall':
                function_calling_convention: bn.CallingConvention = bv.platform.fastcall_calling_convention
            elif token.spelling == '__stdcall':
                function_calling_convention: bn.CallingConvention = bv.platform.stdcall_calling_convention

    function_type = bn.Type.function(func_return_val_type,
                                     func_params,
                                     calling_convention=function_calling_convention,
                                     variable_arguments=variable_arguments
                                     )

    try:
        bv.define_user_type(node.spelling, function_type)
        bn.log.log_debug(f'function_decl: Successfully processed function {node.spelling}')
        return node.spelling, function_type
    except Exception as e:
        bn.log.log_debug(f'function_decl: Failed Processing function {node.spelling} with exception {e}')


def enum_decl(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'enum_decl: Processing enum {node.type.spelling} {node.spelling}')

    enum = bn.Enumeration()
    for enum_member in node.get_children():
        enum.append(enum_member.spelling, enum_member.enum_value)

    try:
        if node.spelling:
            enum_name = node.spelling
        else:
            enum_name = node.type.spelling
        bv.define_user_type(enum_name, bn.Type.enumeration_type(bv.arch, enum))
        bn.log.log_debug(f'enum_decl: Successfully processed enum {node.spelling}')
        return node.spelling, bn.Type.enumeration_type(bv.arch, enum)
    except Exception as e:
        bn.log.log_debug(f'enum_decl: Failed Processing enum {node.spelling} with exception {e}')


def struct_decl(node: Cursor, bv: bn.BinaryView):
    struct = bn.Structure()
    struct.width = node.type.get_size()
    struct.alignment = node.type.get_align()
    if node.spelling:
        struct_name = node.spelling
    else:
        # A struct can be defined anonymously and assigned via a typedef, which means the struct_decl node itself
        # will have no spelling.
        # example: typedef struct {
        #                   DWORD Version;
        #                   GUID Guid;
        #                   SYSTEM_POWER_CONDITION PowerCondition;
        #                   DWORD DataLength;
        #                   BYTE Data[1];
        #                 } SET_POWER_SETTING_VALUE, *PSET_POWER_SETTING_VALUE;
        struct_name = node.type.spelling

    bn.log.log_debug(f'struct_decl: Processing struct {node.spelling}')

    # In order to avoid recursion problems with structs, always define the struct name as a binaryNinja forward decl
    bv.define_user_type(struct_name, bn.Type.structure_type(bn.Structure()))

    # check if struct is a forward declaration within the source code - if it is not a definition, then it is a forward
    # decl, and no fields should be defined at this point.
    if node.is_definition():
        for field in node.type.get_fields():
            bn.log.log_debug(f'struct_decl: Processing struct field {field.spelling}')

            if is_recursive_field(field, bv):
                forward_decl_struct = bn.Structure()
                forward_decl_struct_name = field.type.get_pointee().get_declaration().spelling
                bv.define_user_type(forward_decl_struct_name, bn.Type.structure_type(forward_decl_struct))
                t = bv.get_type_by_name(forward_decl_struct_name)
                struct.append(t, forward_decl_struct_name)
            else:
                var_type = bv.get_type_by_name(field.spelling)
                if not var_type:
                    # Need to define the field type
                    var_name, var_type = define_type(field.get_definition(), bv)
                struct.append(var_type, field.spelling)
            bn.log.log_debug(f'struct_decl: Successfully processed  struct field {field.spelling}')

    try:
        if node.kind == CursorKind.UNION_DECL:
            # set type to union
            struct.type = bn.StructureType.UnionStructureType

        bv.define_user_type(struct_name, bn.Type.structure_type(struct))
        bn.log.log_debug(f'struct_decl: Successfully processed struct {struct_name}')
        return struct_name, bn.Type.structure_type(struct)
    except Exception as e:
        bn.log.log_debug(f'struct_decl: Failed Processing struct {struct_name} with exception {e}')


def define_anonymous_type(node: Cursor, bv: bn.BinaryView) -> bn.Type:
    # An anonymous type must be either a Struct\UNION\ENUM.
    # In order to simplify working with binaryNinja, an anonymized type is de-anonymized:
    # The name of the anonymous type is a hash of its location in the source file prepended by 'anon_'
    bn.log.log_debug(f'define_anonymous_type: Processing {node.type.spelling}')

    struct = bn.Structure()
    struct.width = node.type.get_size()
    struct.alignment = node.type.get_align()
    struct_name = 'anon_' + xxhash.xxh64_hexdigest(node.type.spelling)

    for field in node.type.get_fields():
        bn_field_type = bv.get_type_by_name(field.spelling)
        field_name = field.spelling
        if not bn_field_type:
            # Need to define the field type
            # if field.is_anonymous():
            #    field_name, bn_field_type = define_anonymous_type(field, bv)
            # else:
            field_name, bn_field_type = define_type(field.get_definition(), bv)
        bn.log.log_debug(f'define_anonymous_type: Appending field - {bn_field_type} {field_name}')
        struct.append(bn_field_type, field_name)

    # Check if the underlying struct is a union
    if node.type.kind == TypeKind.ELABORATED:
        if node.type.get_named_type().get_declaration().kind == CursorKind.UNION_DECL:
            # set type to union
            struct.type = bn.StructureType.UnionStructureType

    return struct_name, bn.Type.structure_type(struct)


def is_recursive_field(field: Cursor, bv: bn.BinaryView):
    # Check if a struct field is recursive.
    # If the field is a pointer to a type whos' spelling is the same as the fields' semantic parents' type spelling,
    # then this is a recursive field.
    bn.log.log_debug(f'is_recursive_field: Processing field {field.type.spelling} {field.spelling} \n'
                     f'field.type.kind: {field.type.kind}, field.kind: {field.kind}')

    field_type_declaration_node = None
    if field.type.kind == TypeKind.POINTER:
        pointee_type = field.type.get_pointee()
        if pointee_type.spelling == field.semantic_parent.type.spelling:
            return True
    return False


def field_decl(node: Cursor, bv: bn.BinaryView):
    bn.log.log_debug(f'field_decl: Processing {node.type.spelling} {node.spelling}'
                     f'            node.type.kind: {node.type.kind}, node.kind: {node.kind}')
    try:
        if not is_recursive_field(node, bv):
            if check_if_base_type(node.type):
                field_type, field_name = bv.parse_type_string(f'{node.type.spelling} {node.spelling}')
            else:
                field_name, field_type = define_type(node, bv)
            return str(field_name), field_type
        else:
            bn.log.log_debug(f'field_decl: Unhandled recursive field {node.type.spelling} {node.spelling}')
    except Exception as e:
        bn.log.log_debug(f'field_decl: Failed Processing field {node.type.spelling} {node.spelling}')
