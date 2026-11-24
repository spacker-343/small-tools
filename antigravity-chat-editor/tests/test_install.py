import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capabilities import check_message
from install import repack, unpack
from wire import blob_field as b, encode_varint as v, fields, rewrite


class InstallTests(unittest.TestCase):
    def test_asar_roundtrip_retains_files_and_unpacked_entries(self):
        header = {'files': {'package.json': {'offset': '0', 'size': 0}, 'dist': {'files': {
            'main.js': {'offset': '0', 'size': 0}, 'native.dll': {'size': 20, 'unpacked': True}}}}}
        entries = {'package.json': b'{"version":"999.9.9"}', 'dist/main.js': b'hello'}
        packed = repack(header, entries, {'dist/main.js'})
        actual_header, actual = unpack(packed)
        self.assertEqual(actual, entries)
        self.assertTrue(actual_header['files']['dist']['files']['native.dll']['unpacked'])
        self.assertEqual(repack(actual_header, actual, set()), packed)

    def test_capabilities_do_not_depend_on_version(self):
        field = b(1, b'response') + v(24) + v(1) + v(40) + v(9)
        binary = b'prefix' + b(4, b(1, b'Example') + b(2, field)) + b'suffix'
        check_message(binary, 'Example', {'response': (1, 9)})
        with self.assertRaises(ValueError): check_message(binary, 'Example', {'response': (2, 9)})

    def test_wire_preserves_unknown_fields(self):
        source = v(8) + v(123) + b(9, b'unknown') + b(2, b'old')
        self.assertEqual(rewrite(source, {}), source)
        self.assertEqual(rewrite(source, {2: b'new'}), v(8) + v(123) + b(9, b'unknown') + b(2, b'new'))
        for invalid in (b'\x0a\xff', b'\x00', b'\x0b', b'\x0a\x05x'):
            with self.assertRaises(ValueError): fields(invalid)

if __name__ == '__main__': unittest.main()
