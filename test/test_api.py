# This file is part of CairoSVG
# Copyright © 2010-2015 Kozea
#
# This library is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with CairoSVG.  If not, see <http://www.gnu.org/licenses/>.

"""
CairoSVG API test suite.

"""

import io
import os
import sys
import shutil
import subprocess
import tempfile

import cairocffi as cairo
from nose.tools import assert_raises, eq_

from . import FILES, cairosvg


MAGIC_NUMBERS = {
    'SVG': b'<?xml',
    'PNG': b'\211PNG\r\n\032\n',
    'PDF': b'%PDF',
    'PS': b'%!',
}


def test_formats():
    """Convert to a given format and test that output looks right."""
    svg_filename = FILES[0]
    for format_name in MAGIC_NUMBERS:
        # Use a default parameter value to bind to the current value,
        # not to the variabl as a closure would do.
        def test(format_name=format_name):
            """Test the generation of ``format_name`` images."""
            content = cairosvg.SURFACES[format_name].convert(url=svg_filename)
            assert content.startswith(MAGIC_NUMBERS[format_name])
        test.description = (
            'Test that the output from svg2{} looks like {}'.format(
                format_name.lower(), format_name))
        yield test


def read_file(filename):
    """Shortcut to return the whole content of a file as a byte string."""
    with open(filename, 'rb') as file_object:
        return file_object.read()


def test_api():
    """Test the Python API with various parameters."""
    svg_filename = FILES[0]
    expected_content = cairosvg.svg2png(url=svg_filename)
    # Already tested above: just a sanity check:
    assert expected_content.startswith(MAGIC_NUMBERS['PNG'])

    svg_content = read_file(svg_filename)
    # Read from a byte string
    assert cairosvg.svg2png(svg_content) == expected_content
    assert cairosvg.svg2png(bytestring=svg_content) == expected_content

    with open(svg_filename, 'rb') as file_object:
        # Read from a real file object
        assert cairosvg.svg2png(file_obj=file_object) == expected_content

    file_like = io.BytesIO(svg_content)
    # Read from a file-like object
    assert cairosvg.svg2png(file_obj=file_like) == expected_content

    file_like = io.BytesIO()
    # Write to a file-like object
    cairosvg.svg2png(svg_content, write_to=file_like)
    assert file_like.getvalue() == expected_content

    temp = tempfile.mkdtemp()
    try:
        temp_1 = os.path.join(temp, 'result_1.png')
        with open(temp_1, 'wb') as file_object:
            # Write to a real file object
            cairosvg.svg2png(svg_content, write_to=file_object)
        assert read_file(temp_1) == expected_content

        temp_2 = os.path.join(temp, 'result_2.png')
        # Write to a filename
        cairosvg.svg2png(svg_content, write_to=temp_2)
        assert read_file(temp_2) == expected_content

    finally:
        shutil.rmtree(temp)

    file_like = io.BytesIO()
    assert_raises(TypeError, cairosvg.svg2png, write_to=file_like)


def test_low_level_api():
    """Test the low-level Python API with various parameters."""
    svg_filename = FILES[0]
    expected_content = cairosvg.svg2png(url=svg_filename)

    # Same as above, longer version
    tree = cairosvg.parser.Tree(url=svg_filename)
    file_like = io.BytesIO()
    surface = cairosvg.surface.PNGSurface(tree, file_like, 96)
    surface.finish()
    assert file_like.getvalue() == expected_content

    png_result = cairo.ImageSurface.create_from_png(
        io.BytesIO(expected_content))
    expected_width = png_result.get_width()
    expected_height = png_result.get_height()

    # Abstract surface
    surface = cairosvg.surface.PNGSurface(tree, None, 96)
    assert surface.width == expected_width
    assert surface.height == expected_height
    assert cairo.SurfacePattern(surface.cairo)
    assert_raises(Exception, cairo.SurfacePattern, 'Not a cairo.Surface.')


def test_script():
    """Test the ``cairosvg`` script and the ``main`` function."""
    svg_filename = FILES[0]
    script = os.path.join(os.path.dirname(__file__), '..', 'cairosvg.py')
    expected_png = cairosvg.svg2png(url=svg_filename)
    expected_pdf = cairosvg.svg2pdf(url=svg_filename)

    def test_main(args, exit_=False, input_=None):
        """Test main called with given ``args``.

        If ``exit_`` is ``True``, check that ``SystemExit`` is raised. We then
        assume that the program output is an unicode string.

        If ``input_`` is given, use this stream as input stream.

        """
        sys.argv = ['cairosvg.py'] + args
        old_stdin, old_stdout = sys.stdin, sys.stdout

        output_buffer = io.BytesIO()
        sys.stdout = io.TextIOWrapper(output_buffer)

        if input_:
            kwargs = {'stdin': open(input_, 'rb')}
            sys.stdin = open(input_, 'rb')
            sys.stdin.buffer = sys.stdin
        else:
            kwargs = {}

        if exit_:
            try:
                cairosvg.main()
            except SystemExit:
                pass
            else:
                raise Exception('CairoSVG did not exit')
        else:
            cairosvg.main()

        sys.stdout.flush()
        output = output_buffer.getvalue()
        sys.stdin, sys.stdout = old_stdin, old_stdout
        eq_(output, subprocess.check_output(
            [sys.executable, script] + args, **kwargs))

        return output

    #assert test_main([], exit_=True).startswith(b'Usage: ')
    assert test_main(['--help'], exit_=True).startswith(b'usage: ')
    assert test_main(['--version'], exit_=True).strip() == (
         cairosvg.VERSION.encode('ascii'))
    assert test_main([svg_filename]) == expected_pdf
    assert test_main([svg_filename, '-d', '72', '-f', 'pdf']) == expected_pdf
    assert test_main([svg_filename, '-f', 'png']) == expected_png
    assert test_main(['-'], input_=svg_filename) == expected_pdf

    # Test DPI
    output = test_main([svg_filename, '-d', '10', '-f', 'png'])
    image = cairo.ImageSurface.create_from_png(io.BytesIO(output))
    eq_(image.get_width(), 47)
    eq_(image.get_height(), 20)

    temp = tempfile.mkdtemp()
    try:
        temp_1 = os.path.join(temp, 'result_1')
        # Default to PDF
        assert not test_main([svg_filename, '-o', temp_1])
        assert read_file(temp_1) == expected_pdf

        temp_2 = os.path.join(temp, 'result_2.png')
        # Guess from the file extension
        assert not test_main([svg_filename, '-o', temp_2])
        assert read_file(temp_2) == expected_png

        temp_3 = os.path.join(temp, 'result_3.png')
        # Explicit -f wins
        assert not test_main([svg_filename, '-o', temp_3, '-f', 'pdf'])
        assert read_file(temp_3) == expected_pdf
    finally:
        shutil.rmtree(temp)