"""The Python script that takes table.py and generates C++, Rust ahd .rst code."""
#
# For C++ it generates cxxsettings-generated.cc containing support for old style
# settings plus conversion from old style to new style.
#
# For Rust it generates rust/src/lib.rs, containing the structs with
# CXX and Serde annotations and associated code. rust-preamble-in.rs is
# included before the generated code and rus-bridge-in.rs inside the
# bridge module in the generated lib.rs. Header files generated by CXX
# (lib.rs.h and cxx.h) are copied from deep inside the generated code
# hierarchy into the rust subdir as well.
#
# Two documentation files are generated: ../docs/settings.rst and
# ../docs/yamlsettings.rst.  Each of these files have a preamble as
# well, describing the syntax and giving some examples.
#
# An example table.py entry:
#
# {
# 'name' : "allow_from",
# 'section' : "incoming",
# 'oldname' : "allow-from",
# 'type' : LType.ListSubnets,
# 'default' : "127.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 192.168.0.0/16,
#              172.16.0.0/12, ::1/128, fc00::/7, fe80::/10",
# 'help' : "If set, only allow these comma separated netmasks to recurse",
# 'doc' : """
#  """
# }
#
# The fields are:
#
#   name: short name within section, also yaml key, must be unique within section and not a C++ or
#            Rust keyword
#   section: yaml section
#   oldname: name in old style settings, must be unique globally
#   type : logical type (leave the space before the : as pylint is buggy parsing lines with type
#            immediately followed by colon)x
#   default: default value, use 'true' and 'false' for Booleans
#   help: short help text
#   doc: the docs text will be put here
#   doc-rst: optional .rst annotations, typically used for ..version_added/changed::
#   doc-new: optional docs text specific to YAML format, e.g. not talking about comma separated
#            lists and giving YAML examples. (optional)
#   skip-yamL: optional if this key is present, the field wil be skipped in the new style settings.
#            Use for deprecated settings, the generated code will use deprecated map from arg()
#            to handle old names when converting .conf to .yml
#   versionadded: string or list of strings
#
# The above struct will generate in cxxsettings-generated.cc:
#
#   ::arg().set("allow-from", "If set, only allow these comma separated netmasks to recurse") = \
#     "127.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, ...";
#
# For Rust, it will generate a field `allow_from' within struct Incoming.
# As CXX places restrictions on the Serde code (which means we cannot
# use Serde extensions) we have to handle the default value and "skip if
# default" functions ourselves.
#
# There are three cases:
#
# 1. Default is the same as the Rust default for the type
# In that case, the field becomes:
#
# #[serde(default, skip_serializing_if = "crate::is_default")]
# nameoffield: bool
#
# 2. Type is primitive and default value is different from the Rust default, e.g.:
#
# #[serde(default = "crate::U64::<2000>::value",
#     skip_serializing_if = "crate::U64::<2000>::is_equal")]
# nameoffield: u64
#
# U64<constant> is implemented in rust/src/helpers.rs
#
# 3. Type is not primitive and has default value different from Rust default, e.g.:
#
# [serde(default = "crate::default_value_incoming_allow_from",
#     skip_serializing_if = "crate::default_value_equal_incoming_allow_from")]
# allow_from: Vec<String>
#
# The two functions default_value_incoming_allow_from() and
# default_value_equal_incoming_allow_from() are also generated.
#
from enum import Enum
from enum import auto
import os
import re
import sys

class LType(Enum):
    """The type we handle in table.py"""
    Bool = auto()
    Command = auto()
    Double = auto()
    ListAuthZones = auto()
    ListSocketAddresses = auto()
    ListStrings = auto()
    ListSubnets = auto()
    ListForwardZones = auto()
    String = auto()
    Uint64 = auto()

def get_olddoc_typename(typ):
    """Given a type from table.py, return the old-style type name"""
    if typ == LType.Bool:
        return 'Boolean'
    if typ == LType.Uint64:
        return 'Integer'
    if typ == LType.Double:
        return 'Double'
    if typ == LType.String:
        return 'String'
    if typ == LType.ListSocketAddresses:
        return 'Comma separated list or IPs of IP:port combinations'
    if typ == LType.ListSubnets:
        return 'Comma separated list of IP addresses or subnets, negation supported'
    if typ == LType.ListStrings:
        return 'Comma separated list of strings'
    if typ == LType.ListForwardZones:
        return 'Comma separated list of \'zonename=IP\' pairs'
    if typ == LType.ListAuthZones:
        return 'Comma separated list of \'zonename=filename\' pairs'
    return 'Unknown' + str(typ)

def get_newdoc_typename(typ):
    """Given a type from table.py, return the new-style type name"""
    if typ == LType.Bool:
        return 'Boolean'
    if typ == LType.Uint64:
        return 'Integer'
    if typ == LType.Double:
        return 'Double'
    if typ == LType.String:
        return 'String'
    if typ == LType.ListSocketAddresses:
        return 'Sequence of `Socket Address`_ (IP or IP:port combinations)'
    if typ == LType.ListSubnets:
        return 'Sequence of `Subnet`_ (IP addresses or subnets, negation supported)'
    if typ == LType.ListStrings:
        return 'Sequence of strings'
    if typ == LType.ListForwardZones:
        return 'Sequence of `Forward Zone`_'
    if typ == LType.ListAuthZones:
        return 'Sequence of `Auth Zone`_'
    return 'Unknown' + str(typ)

def get_default_olddoc_value(typ, val):
    """Given a type and a value from table.py return the old doc representation of the value"""
    if typ == LType.Bool:
        if val == 'false':
            return 'no'
        return 'yes'
    if val == '':
        return '(empty)'
    return val

def get_default_newdoc_value(typ, val):
    """Given a type and a value from table.py return the new doc representation of the value"""
    if typ in (LType.Bool, LType.Uint64, LType.Double):
        return '``' + val + '``'
    if typ == LType.String and val == '':
        return '(empty)'
    if typ == LType.String:
        return '``' + val + '``'
    parts = re.split('[ \t,]+', val)
    if len(parts) > 0:
        ret = ''
        for part in parts:
            if part == '':
                continue
            if ret != '':
                ret += ', '
            if ':' in part or '!' in part:
                ret += "'" + part + "'"
            else:
                ret += part
    else:
        ret = ''
    return '``[' + ret + ']``'

def get_rust_type(typ):
    """Determine which Rust type is used for a logical type"""
    if typ == LType.Bool:
        return 'bool'
    if typ == LType.Uint64:
        return 'u64'
    if typ == LType.Double:
        return 'f64'
    if typ == LType.String:
        return 'String'
    if typ == LType.ListSocketAddresses:
        return 'Vec<String>'
    if typ == LType.ListSubnets:
        return 'Vec<String>'
    if typ == LType.ListStrings:
        return 'Vec<String>'
    if typ == LType.ListForwardZones:
        return 'Vec<ForwardZone>'
    if typ == LType.ListAuthZones:
        return 'Vec<AuthZone>'
    return 'Unknown' + str(typ)

def quote(arg):
    """Return a quoted string"""
    return '"' + arg + '"'

def gen_cxx_defineoldsettings(file, entries):
    """Generate C++ code to declare old-style settings"""
    file.write('void pdns::settings::rec::defineOldStyleSettings()\n{\n')
    for entry in entries:
        helptxt = quote(entry['help'])
        oldname = quote(entry['oldname'])
        if entry['type'] == LType.Bool:
            if entry['default'] == "true":
                cxxdef = "yes"
            else:
                cxxdef = "no"
            cxxdef = quote(cxxdef)
            file.write(f"  ::arg().setSwitch({oldname}, {helptxt}) = {cxxdef};\n")
        elif entry['type'] == LType.Command:
            file.write(f"  ::arg().setCmd({oldname}, {helptxt});\n")
        else:
            cxxdef = 'SYSCONFDIR' if entry['default'] == 'SYSCONFDIR' else quote(entry['default'])
            file.write(f"  ::arg().set({oldname}, {helptxt}) = {cxxdef};\n")
    file.write('}\n\n')

def gen_cxx_oldstylesettingstobridgestruct(file, entries):
    """Generate C++ code the convert old-style settings to new-style struct"""
    file.write('void pdns::settings::rec::oldStyleSettingsToBridgeStruct')
    file.write('(Recursorsettings& settings)\n{\n')
    for entry in entries:
        if entry['type'] == LType.Command:
            continue
        if 'skip-yaml' in entry:
            continue
        rust_type = get_rust_type(entry['type'])
        name = entry['name']
        oldname = entry['oldname']
        section = entry['section']
        file.write(f'  settings.{section}.{name} = ')
        if rust_type == 'bool':
            file.write(f'arg().mustDo("{oldname}")')
        elif rust_type == 'u64':
            file.write(f'static_cast<uint64_t>(arg().asNum("{oldname}"))')
        elif rust_type == 'f64':
            file.write(f'arg().asDouble("{oldname}")')
        elif rust_type == 'String':
            file.write(f'arg()["{oldname}"]')
        elif rust_type == 'Vec<String>':
            file.write(f'getStrings("{oldname}")')
        elif rust_type == 'Vec<ForwardZone>':
            file.write(f'getForwardZones("{oldname}")')
        elif rust_type == 'Vec<AuthZone>':
            file.write(f'getAuthZones("{oldname}")')
        else:
            file.write(f'Unknown type {rust_type}\n')
        file.write(';\n')
    file.write('}\n\n')

def gen_cxx_oldkvtobridgestruct(file, entries):
    """Generate C++ code for oldKVToBridgeStruct"""
    file.write('// Inefficient, but only meant to be used for one-time conversion purposes\n')
    file.write('bool pdns::settings::rec::oldKVToBridgeStruct(std::string& key, ')
    file.write('const std::string& value, ::rust::String& section, ::rust::String& fieldname, ')
    file.write('::rust::String& type_name, pdns::rust::settings::rec::Value& rustvalue)')
    file.write('{ // NOLINT(readability-function-cognitive-complexity)\n')
    file.write('  if (const auto newname = arg().isDeprecated(key); !newname.empty()) {\n')
    file.write('    key = newname;\n')
    file.write('  }\n')
    for entry in entries:
        if entry['type'] == LType.Command:
            continue
        if 'skip-yaml' in entry:
            continue
        rust_type = get_rust_type(entry['type'])
        extra = ''
        if entry['oldname'] == 'forward-zones-recurse':
            extra = ', true'
        name = entry['name']
        section = entry['section']
        oldname = entry['oldname']
        file.write(f'  if (key == "{oldname}") {{\n')
        file.write(f'    section = "{section}";\n')
        file.write(f'    fieldname = "{name}";\n')
        file.write(f'    type_name = "{rust_type}";\n')
        if rust_type == 'bool':
            file.write(f'    to_yaml(rustvalue.bool_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        elif rust_type == 'u64':
            file.write(f'    to_yaml(rustvalue.u64_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        elif rust_type == 'f64':
            file.write(f'    to_yaml(rustvalue.f64_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        elif rust_type == 'String':
            file.write(f'    to_yaml(rustvalue.string_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        elif rust_type == 'Vec<String>':
            file.write(f'    to_yaml(rustvalue.vec_string_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        elif rust_type == 'Vec<ForwardZone>':
            file.write(f'    to_yaml(rustvalue.vec_forwardzone_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        elif rust_type == 'Vec<AuthZone>':
            file.write(f'    to_yaml(rustvalue.vec_authzone_val, value{extra});\n')
            file.write('    return true;\n  }\n')
        else:
            file.write(f'Unknown type {rust_type}\n')
    file.write('  return false;\n')
    file.write('}\n\n')

def gen_cxx_brigestructtoldstylesettings(file, entries):
    """Generate C++ Code for bridgeStructToOldStyleSettings"""
    file.write('void pdns::settings::rec::bridgeStructToOldStyleSettings')
    file.write('(const Recursorsettings& settings)\n{\n')
    for entry in entries:
        if entry['type'] == LType.Command:
            continue
        if 'skip-yaml' in entry:
            continue
        section = entry['section'].lower()
        name = entry['name']
        oldname = entry['oldname']
        file.write(f'  ::arg().set("{oldname}") = ')
        file.write(f'to_arg(settings.{section}.{name});\n')
    file.write('}\n')

def gen_cxx(entries):
    """Generate the C++ code from the defs in table.py"""
    with open('cxxsettings-generated.cc', mode='w', encoding="UTF-8") as file:
        file.write('// THIS IS A GENERATED FILE. DO NOT EDIT. SOURCE: see settings dir\n\n')
        file.write('#include "arguments.hh"\n')
        file.write('#include "cxxsettings.hh"\n')
        file.write('#include "cxxsettings-private.hh"\n\n')
        gen_cxx_defineoldsettings(file, entries)
        gen_cxx_oldstylesettingstobridgestruct(file, entries)
        gen_cxx_oldkvtobridgestruct(file, entries)
        gen_cxx_brigestructtoldstylesettings(file, entries)

def is_value_rust_default(typ, value):
    """Is a value (represented as string) the same as its corresponding Rust default?"""
    if typ == 'bool':
        return value == 'false'
    if typ == 'u64':
        return value in ('0', '')
    if typ == 'f64':
        return value == '0.0'
    if typ == 'String':
        return value == ''
    return False

def gen_rust_forwardzonevec_default_functions(name):
    """Generate Rust code for the default handling of a vector for ForwardZones"""
    ret = f'// DEFAULT HANDLING for {name}\n'
    ret += f'fn default_value_{name}() -> Vec<recsettings::ForwardZone> {{\n'
    ret += '    Vec::new()\n'
    ret += '}\n'
    ret += f'fn default_value_equal_{name}(value: &Vec<recsettings::ForwardZone>)'
    ret += '-> bool {\n'
    ret += f'    let def = default_value_{name}();\n'
    ret += '    &def == value\n'
    ret += '}\n\n'
    return ret

def gen_rust_authzonevec_default_functions(name):
    """Generate Rust code for the default handling of a vector for AuthZones"""
    ret = f'// DEFAULT HANDLING for {name}\n'
    ret += f'fn default_value_{name}() -> Vec<recsettings::AuthZone> {{\n'
    ret += '    Vec::new()\n'
    ret += '}\n'
    ret += f'fn default_value_equal_{name}(value: &Vec<recsettings::AuthZone>)'
    ret += '-> bool {\n'
    ret += f'    let def = default_value_{name}();\n'
    ret += '    &def == value\n'
    ret += '}\n\n'
    return ret

# Example snippet generated
# fn default_value_general_query_local_address() -> Vec<String> {
#    vec![String::from("0.0.0.0"), ]
#}
#fn default_value_equal_general_query_local_address(value: &Vec<String>) -> bool {
#    let def = default_value_general_query_local_address();
#    &def == value
#}
def gen_rust_stringvec_default_functions(entry, name):
    """Generate Rust code for the default handling of a vector for Strings"""
    ret = f'// DEFAULT HANDLING for {name}\n'
    ret += f'fn default_value_{name}() -> Vec<String> {{\n'
    parts = re.split('[ \t,]+', entry['default'])
    if len(parts) > 0:
        ret += '    vec![\n'
        for part in  parts:
            if part == '':
                continue
            ret += f'        String::from({quote(part)}),\n'
        ret += '    ]\n'
    else:
        ret  += '    vec![]\n'
    ret += '}\n'
    ret += f'fn default_value_equal_{name}(value: &Vec<String>) -> bool {{\n'
    ret += f'    let def = default_value_{name}();\n'
    ret += '    &def == value\n'
    ret += '}\n\n'
    return ret

def gen_rust_default_functions(entry, name, rust_type):
    """Generate Rust code for the default handling"""
    if entry['type'] in (LType.ListSocketAddresses, LType.ListSubnets, LType.ListStrings):
        return gen_rust_stringvec_default_functions(entry, name)
    if entry['type'] == LType.ListForwardZones:
        return gen_rust_forwardzonevec_default_functions(name)
    if entry['type'] == LType.ListAuthZones:
        return gen_rust_authzonevec_default_functions(name)
    ret = f'// DEFAULT HANDLING for {name}\n'
    ret += f'fn default_value_{name}() -> {rust_type} {{\n'
    rustdef = 'env!("SYSCONFDIR")' if entry['default'] == 'SYSCONFDIR' else quote(entry['default'])
    ret += f"    String::from({rustdef})\n"
    ret += '}\n'
    if rust_type == 'String':
        rust_type = 'str'
    ret += f'fn default_value_equal_{name}(value: &{rust_type})'
    ret += '-> bool {\n'
    ret += f'    value == default_value_{name}()\n'
    ret += '}\n\n'
    return ret

def write_rust_field(file, entry, default_funcs):
    """Generate Rust code for a field with Serde annotations"""
    rust_type = get_rust_type(entry['type'])
    the_default = entry['default']
    is_rust_default = is_value_rust_default(rust_type, the_default)
    if is_rust_default:
        file.write('        #[serde(default, skip_serializing_if = "crate::is_default")]\n')
    else:
        if entry['type'] == LType.Bool:
            file.write('        #[serde(default = "crate::Bool::<true>::value", ')
            file.write('skip_serializing_if = "crate::if_true")]\n')
        elif entry['type'] == LType.Uint64:
            file.write(f'        #[serde(default = "crate::U64::<{the_default}>::value", ')
            file.write(f'skip_serializing_if = "crate::U64::<{the_default}>::is_equal")]\n')
        else:
            basename = entry['section'] + '_' + entry['name']
            file.write(f'        #[serde(default = "crate::default_value_{basename}", ')
            file.write(f'skip_serializing_if = "crate::default_value_equal_{basename}")]\n')
            default_funcs.append(gen_rust_default_functions(entry, basename, rust_type))
    file.write(f"        {entry['name']}: {rust_type},\n\n")

def write_rust_section(file, section, entries, default_funcs):
    """Generate Rust code for a Section with Serde annotations"""
    file.write(f'    // SECTION {section.capitalize()}\n')
    file.write('    #[derive(Deserialize, Serialize, Debug, PartialEq)]\n')
    file.write('    #[serde(deny_unknown_fields)]\n')
    file.write(f'    pub struct {section.capitalize()} {{\n')
    for entry in entries:
        if entry['section'] != section:
            continue
        if entry['type'] == LType.Command:
            continue
        if 'skip-yaml' in entry:
            continue
        write_rust_field(file, entry, default_funcs)
    file.write(f'    }}\n    // END SECTION {section.capitalize()}\n\n')

#
# Each section als has a Default implementation, so that a section with all entries having a default
# value does not get generated into a yaml section. Such a tarit looks like:
#
#impl Default for recsettings::ForwardZone {
#    fn default() -> Self {
#        let deserialized: recsettings::ForwardZone = serde_yaml::from_str("").unwrap();
#        deserialized
#    }
#}

def write_rust_default_trait_impl(file, section):
    """Generate Rust code for the default Trait for a section"""
    file.write(f'impl Default for recsettings::{section.capitalize()} {{\n')
    file.write('    fn default() -> Self {\n')
    file.write('        let deserialized: recsettings::')
    file.write(f'{section.capitalize()} = serde_yaml::from_str("").unwrap();\n')
    file.write('        deserialized\n')
    file.write('    }\n')
    file.write('}\n\n')

def write_validator(file, section, entries):
    """Generate Rust code for the Validator Trait for a section"""
    file.write(f'impl Validate for recsettings::{section.capitalize()} {{\n')
    file.write('    fn validate(&self) -> Result<(), ValidationError> {\n')
    for entry in entries:
        if entry['section'] != section:
            continue
        name = entry['name'].lower()
        typ = entry['type']
        if typ == LType.ListSubnets:
            validator = 'validate_subnet'
        elif typ == LType.ListSocketAddresses:
            validator = 'validate_socket_address'
        elif typ == LType.ListForwardZones:
            validator = '|field, element| element.validate(field)'
        elif typ == LType.ListAuthZones:
            validator = '|field, element| element.validate(field)'
        else:
            continue
        file.write(f'        let fieldname = "{section.lower()}.{name}".to_string();\n')
        file.write(f'        validate_vec(&fieldname, &self.{name}, {validator})?;\n')
    file.write('        Ok(())\n')
    file.write('    }\n')
    file.write('}\n\n')

def write_rust_merge_trait_impl(file, section, entries):
    """Generate Rust code for the Merge Trait for a section"""
    file.write(f'impl Merge for recsettings::{section.capitalize()} {{\n')
    file.write('    fn merge(&mut self, rhs: &mut Self, map: Option<&serde_yaml::Mapping>) {\n')
    file.write('        if let Some(m) = map {\n')
    for entry in entries:
        if entry['section'] != section:
            continue
        if 'skip-yaml' in entry:
            continue
        rtype = get_rust_type(entry['type'])
        name = entry['name']
        file.write(f'            if m.contains_key("{name}") {{\n')
        if rtype in ('bool', 'u64', 'f64', 'String'):
            file.write(f'                self.{name} = rhs.{name}.to_owned();\n')
        else:
            file.write(f'                if is_overriding(m, "{name}") || ')
            file.write(f'self.{name} == DEFAULT_CONFIG.{section}.{name} {{\n')
            file.write(f'                    self.{name}.clear();\n')
            file.write('                }\n')
            file.write(f'                merge_vec(&mut self.{name}, &mut rhs.{name});\n')
        file.write('            }\n')
    file.write('        }\n')
    file.write('    }\n')
    file.write('}\n\n')

def gen_rust(entries):
    """Generate Rust code all entries"""
    def_functions = []
    sections = {}
    with open('rust/src/lib.rs', mode='w', encoding='UTF-8') as file:
        file.write('// THIS IS A GENERATED FILE. DO NOT EDIT. SOURCE: see settings dir\n')
        file.write('// START INCLUDE rust-preable-in.rs\n')
        with open('rust-preamble-in.rs', mode='r', encoding='UTF-8') as pre:
            file.write(pre.read())
            file.write('// END INCLUDE rust-preamble-in.rs\n\n')

        file.write('#[cxx::bridge(namespace = "pdns::rust::settings::rec")]\n')
        file.write('mod recsettings {\n')
        with open('rust-bridge-in.rs', mode='r', encoding='UTF-8') as bridge:
            file.write('    // START INCLUDE rust-bridge-in.rs\n')
            for line in bridge:
                file.write('    ' + line)

        file.write('    // END INCLUDE rust-bridge-in.rs\n\n')
        for entry in entries:
            if entry['section'] == 'commands':
                continue
            sections[entry['section']] = entry['section']

        for section in sections:
            write_rust_section(file, section, entries, def_functions)

        file.write('    #[derive(Serialize, Deserialize, Debug)]\n')
        file.write('    #[serde(deny_unknown_fields)]\n')
        file.write('    pub struct Recursorsettings {\n')
        for section in sections:
            file.write('        #[serde(default, skip_serializing_if = "crate::is_default")]\n')
            file.write(f'        {section.lower()}: {section.capitalize()},\n')
        file.write('}  // End of generated structs\n')
        file.write('}\n')

        for section in sections:
            write_rust_default_trait_impl(file, section)
        write_rust_default_trait_impl(file, 'Recursorsettings')

        for section in sections:
            write_validator(file, section, entries)

        file.write('impl crate::recsettings::Recursorsettings {\n')
        file.write('    fn validate(&self) -> Result<(), ValidationError> {\n')
        for section in sections:
            file.write(f'        self.{section.lower()}.validate()?;\n')
        file.write('        Ok(())\n')
        file.write('    }\n')
        file.write('}\n\n')

        for section in sections:
            write_rust_merge_trait_impl(file, section, entries)

        file.write('impl Merge for crate::recsettings::Recursorsettings {\n')
        file.write('    fn merge(&mut self, rhs: &mut Self, map: Option<&serde_yaml::Mapping>) {\n')
        file.write('        if let Some(m) = map {\n')
        for section in sections:
            file.write(f'            if let Some(s) = m.get("{section}") {{\n')
            file.write('                if s.is_mapping() {\n')
            file.write((f'                    self.{section}.merge(&mut rhs.{section},'
                       ' s.as_mapping());\n'))
            file.write('                }\n')
            file.write('            }\n')
        file.write('        }\n')
        file.write('    }\n')
        file.write('}\n\n')

        for entry in def_functions:
            file.write(entry)
        file.close()

def gen_docs_meta(file, entry, name, is_tuple):
    """Write .. versionadded:: and related entries"""
    if name in entry:
        val = entry[name]
        if not isinstance(val, list):
            val = [val]
        for vers in val:
            if is_tuple:
                file.write(f'.. {name}:: {vers[0]}\n\n')
                file.write(f'  {vers[1].strip()}\n')
            else:
                file.write(f'.. {name}:: {vers}\n')

def gen_oldstyle_docs(entries):
    """Write old style docs"""
    with open('../docs/settings.rst', mode='w', encoding='UTF-8') as file:
        file.write('.. THIS IS A GENERATED FILE. DO NOT EDIT. SOURCE: see settings dir\n')
        file.write('   START INCLUDE docs-old-preamble-in.rst\n\n')
        with open('docs-old-preamble-in.rst', mode='r', encoding='UTF-8') as pre:
            file.write(pre.read())
            file.write('.. END INCLUDE docs-old-preamble-in.rst\n\n')

        for entry in entries:
            if entry['type'] == LType.Command:
                continue
            if entry['doc'].strip() == 'SKIP':
                continue
            oldname = entry['oldname']
            section = entry['section']
            file.write(f'.. _setting-{oldname}:\n\n')
            file.write(f'``{oldname}``\n')
            dots = '~' * (len(entry['oldname']) + 4)
            file.write(f'{dots}\n')
            gen_docs_meta(file, entry, 'versionadded', False)
            gen_docs_meta(file, entry, 'versionchanged', True)
            gen_docs_meta(file, entry, 'deprecated', True)
            if 'doc-rst' in entry:
                file.write(entry['doc-rst'].strip())
                file.write('\n')
            file.write('\n')
            typ = get_olddoc_typename(entry['type'])
            file.write(f'-  {typ}\n')
            if 'docdefault' in entry:
                file.write(f"-  Default: {entry['docdefault']}\n\n")
            else:
                file.write((f"-  Default: "
                            f"{get_default_olddoc_value(entry['type'], entry['default'])}\n\n"))
            if 'skip-yaml' in entry:
                file.write('- YAML setting does not exist\n\n')
            else:
                file.write(f"- YAML setting: :ref:`setting-yaml-{section}.{entry['name']}`\n\n")
            file.write(entry['doc'].strip())
            file.write('\n\n')

def fixxrefs(entries, arg):
    """Docs in table refer to old style names, we modify them to ref to new style"""
    matches = re.findall(':ref:`setting-(.*?)`', arg)
    # We want to replace longest match first, to avoid a short match modifying a long one
    matches = sorted(matches, key = lambda x: -len(x))
    for match in matches:
        for entry in entries:
            if entry['oldname'] == match:
                key = ':ref:`setting-' + match
                repl = ':ref:`setting-yaml-' + entry['section'] + '.' + entry['name']
                arg = arg.replace(key, repl)
    return arg

def gen_newstyle_docs(argentries):
    """Write new style docs"""
    entries = sorted(argentries, key = lambda entry: [entry['section'], entry['name']])
    with open('../docs/yamlsettings.rst', 'w', encoding='utf-8') as file:
        file.write('.. THIS IS A GENERATED FILE. DO NOT EDIT. SOURCE: see settings dir\n')
        file.write('   START INCLUDE docs-new-preamble-in.rst\n\n')
        with open('docs-new-preamble-in.rst', mode='r', encoding='utf-8') as pre:
            file.write(pre.read())
            file.write('.. END INCLUDE docs-new-preamble-in.rst\n\n')

        for entry in entries:
            if entry['type'] == LType.Command:
                continue
            if entry['doc'].strip() == 'SKIP':
                continue
            if 'skip-yaml' in entry:
                continue
            section = entry['section']
            name = entry['name']
            fullname = section + '.' + name
            file.write(f'.. _setting-yaml-{fullname}:\n\n')
            file.write(f'``{fullname}``\n')
            dots = '^' * (len(fullname) + 4)
            file.write(f'{dots}\n')
            gen_docs_meta(file, entry, 'versionadded', False)
            gen_docs_meta(file, entry, 'versionchanged', True)
            gen_docs_meta(file, entry, 'deprecated', True)
            if 'doc-rst' in entry:
                file.write(fixxrefs(entries, entry['doc-rst'].strip()))
                file.write('\n')
            file.write('\n')
            file.write(f"-  {get_newdoc_typename(entry['type'])}\n")
            if 'docdefault' in entry:
                file.write(f"-  Default: {entry['docdefault']}\n\n")
            else:
                file.write((f"-  Default: "
                            f"{get_default_newdoc_value(entry['type'], entry['default'])}\n\n"))
            file.write(f"- Old style setting: :ref:`setting-{entry['oldname']}`\n\n")
            if 'doc-new' in entry:
                file.write(fixxrefs(entries, entry['doc-new'].strip()))
            else:
                file.write(fixxrefs(entries, entry['doc'].strip()))
            file.write('\n\n')

RUNTIME = '*runtime determined*'

def generate():
    """Read table, validate and generate C++, Rst and .rst files"""
    # read table
    with open('table.py', mode='r', encoding="utf-8") as file:
        entries = eval(file.read())

    for entry in entries:
        the_oldname = entry['name'].replace('_', '-')
        if 'oldname' in entry:
            if entry['oldname'] == the_oldname:
                sys.stderr.write(f"Redundant old name {entry['oldname']}\n")
        else:
            entry['oldname'] = the_oldname

    dupcheck1 = {}
    dupcheck2 = {}
    for entry in entries:
        if entry['oldname'] in dupcheck1:
            sys.stderr.write(f"duplicate entries with oldname = {entry['oldname']}\n")
            sys.exit(1)
        if entry['section'] + '.' + entry['name'] in dupcheck2:
            sys.stderr.write((f"duplicate entries with section.name = "
                              f"{entry['section']}.{ entry['name']}\n"))
            sys.exit(1)
        dupcheck1[entry['oldname']] = True
        dupcheck2[entry['section'] + '.' + entry['name']] = True
    # And generate C++, Rust and docs code based on table
    gen_cxx(entries)
    gen_rust(entries)
    # Avoid generating doc files in a sdist based build
    if os.path.isdir('../docs'):
        gen_oldstyle_docs(entries)
        gen_newstyle_docs(entries)

generate()