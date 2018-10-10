# This file is part of CairoSVG
# Copyright © 2010-2018 Kozea
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
SVG Parser.

"""

import gzip
import re
from urllib.parse import urlunparse
from xml.etree.ElementTree import Element

import cssselect2
from defusedxml import ElementTree

from . import css
from .features import match_features
from .helpers import flatten, pop_rotation, rotations
from .url import fetch, parse_url, read_url

# 'display' is actually inherited but handled differently because some markers
# are part of a none-displaying group (see test painting-marker-07-f.svg)
NOT_INHERITED_ATTRIBUTES = frozenset((
    'clip',
    'clip-path',
    'display',
    'filter',
    'height',
    'id',
    'mask',
    'opacity',
    'overflow',
    'rotate',
    'stop-color',
    'stop-opacity',
    'style',
    'transform',
    'viewBox',
    'width',
    'x',
    'y',
    'dx',
    'dy',
    '{http://www.w3.org/1999/xlink}href',
))

COLOR_ATTRIBUTES = frozenset((
    'fill',
    'flood-color',
    'lighting-color',
    'stop-color',
    'stroke',
))


def handle_white_spaces(string, preserve):
    """Handle white spaces in text nodes.

    See http://www.w3.org/TR/SVG/text.html#WhiteSpace

    """
    if not string:
        return ''
    if preserve:
        return re.sub('[\n\r\t]', ' ', string)
    else:
        string = re.sub('[\n\r]', '', string)
        string = re.sub('\t', ' ', string)
        return re.sub(' +', ' ', string)


def normalize_style_declaration(name, value):
    """Normalize style declaration consisting of name/value pair.

    Names are always case insensitive, make all lowercase.
    Values are case insensitive in most cases. Adapt for 'specials':
        id - case sensitive identifier
        class - case sensitive identifier(s)
        font-family - case sensitive name(s)
        font - shorthand in which font-family is case sensitive
        any declaration with url in value - url is case sensitive

    """
    name = name.strip().lower()
    value = value.strip()
    if name in CASE_SENSITIVE_STYLE_METHODS:
        value = CASE_SENSITIVE_STYLE_METHODS[name](value)
    else:
        value = value.lower()

    return name, value


def normalize_noop_style_declaration(value):
    """No-operation for normalization where value is case sensitive.

    This is actually the exception to the rule. Normally value will be made
    lowercase (see normalize_style_declaration above).

    """
    return value


def normalize_url_style_declaration(value):
    """Normalize style declaration, but keep URL's as-is.

    Lowercase everything except for the URL.

    """
    regex_style = re.compile(r"""
        (.*?)                               # non-URL part (will be normalized)
        (?:
            url\(\s*                        # url(<whitespace>
                (?:
                      "(?:\\.|[^"])*"       # "<url>"
                    | \'(?:\\.|[^\'])*\'    # '<url>'
                    | (?:\\.|[^\)])*        # <url>
                )
            \s*\)                           # <whitespace>)
            |$
        )
    """, re.IGNORECASE | re.VERBOSE)
    for match in regex_style.finditer(value):
        value_start = value[:match.start()] if match.start() > 0 else ''
        normalized_value = match.group(1).lower()
        value_end = value[match.start() + len(normalized_value):]
        value = value_start + normalized_value + value_end
    return value


def normalize_font_style_declaration(value):
    """Make first part of font style declaration lowercase (case insensitive).

    Lowercase first part of declaration. Only the font name is case sensitive.
    The font name is at the end of the declaration and can be 'recognized'
    by being preceded by a size or line height. There can actually be multiple
    names. So the first part is 'calculated' by selecting everything up to and
    including the last valid token followed by a size or line height (both
    starting with a number). A valid token is either a size/length or an
    identifier.

    See http://www.w3.org/TR/css-fonts-3/#font-prop

    """
    return re.sub(r"""
        ^(
            (\d[^\s,]*|\w[^\s,]*)   # <size>, <length> or <identifier>
            (\s+|\s*,\s*)           # <whitespace> and/or comma
        )*                          # Repeat until last
        \d[^\s,]*                   # <size> or <line-height>
    """, lambda match: match.group().lower(), value, 0, re.VERBOSE)


class Node(dict):
    """SVG node with dict-like properties and children."""

    def __init__(self, element, style, url_fetcher, parent=None,
                 parent_children=False, url=None, unsafe=False):
        """Create the Node from ElementTree ``node``, with ``parent`` Node."""
        super().__init__()
        self.children = ()

        self.root = False

        node = element.etree_element
        self.element = element
        self.style = style
        self.tag = (
            element.local_name
            if element.namespace_url in ('', 'http://www.w3.org/2000/svg') else
            '{%s}%s' % (element.namespace_url, element.local_name))
        self.text = node.text
        self.url_fetcher = url_fetcher
        self.unsafe = unsafe

        # Only set xml_tree if it's not been set before (ie. if node is a tree)
        self.xml_tree = getattr(self, 'xml_tree', node)

        # Inherits from parent properties
        if parent is not None:
            self.update([
                (attribute, parent[attribute]) for attribute in parent
                if attribute not in NOT_INHERITED_ATTRIBUTES])
            self.url = url or parent.url
            self.parent = parent
        else:
            self.url = getattr(self, 'url', None)
            self.parent = getattr(self, 'parent', None)

        self.update(self.xml_tree.attrib)

        # Apply CSS rules
        style_attr = node.get('style')
        if style_attr:
            normal_attr, important_attr = css.parse_declarations(style_attr)
        else:
            normal_attr = []
            important_attr = []
        normal_matcher, important_matcher = style
        normal = [rule[-1] for rule in normal_matcher.match(element)]
        important = [rule[-1] for rule in important_matcher.match(element)]
        for declaration_lists in (
                normal, [normal_attr], important, [important_attr]):
            for declarations in declaration_lists:
                for name, value in declarations:
                    self[name] = value.strip()

        # Replace currentColor by a real color value
        for attribute in COLOR_ATTRIBUTES:
            if self.get(attribute) == 'currentColor':
                self[attribute] = self.get('color', 'black')

        # Replace inherit by the parent value
        for attribute in [
                attribute for attribute in self
                if self[attribute] == 'inherit']:
            if parent is not None and attribute in parent:
                self[attribute] = parent.get(attribute)
            else:
                del self[attribute]

        # Manage text by creating children
        if self.tag in ('text', 'textPath', 'a'):
            self.children, _ = self.text_children(
                element, trailing_space=True, text_root=True)

        if parent_children:
            self.children = [
                Node(child.element, style, self.url_fetcher, parent=self,
                     unsafe=self.unsafe)
                for child in parent.children]
        elif not self.children:
            self.children = []
            for child in element.iter_children():
                if match_features(child.etree_element):
                    self.children.append(
                        Node(child, style, self.url_fetcher, parent=self,
                             unsafe=self.unsafe))
                    if self.tag == 'switch':
                        break

    def fetch_url(self, url, resource_type):
        return read_url(url, self.url_fetcher, resource_type)

    def text_children(self, element, trailing_space, text_root=False):
        """Create children and return them."""
        children = []
        space = '{http://www.w3.org/XML/1998/namespace}space'
        preserve = self.get(space) == 'preserve'
        self.text = handle_white_spaces(element.etree_element.text, preserve)
        if trailing_space and not preserve:
            self.text = self.text.lstrip(' ')
        original_rotate = rotations(self)
        rotate = list(original_rotate)
        if original_rotate:
            pop_rotation(self, original_rotate, rotate)
        if self.text:
            trailing_space = self.text.endswith(' ')
        for child_element in element.iter_children():
            child = child_element.etree_element
            if child.tag == '{http://www.w3.org/2000/svg}tref':
                url = parse_url(child.get(
                    '{http://www.w3.org/1999/xlink}href')).geturl()
                child_tree = Tree(
                    url=url, url_fetcher=self.url_fetcher, parent=self,
                    unsafe=self.unsafe)
                child_tree.clear()
                child_tree.update(self)
                child_node = Node(
                    child_element, self.style, self.url_fetcher,
                    parent=child_tree, parent_children=True,
                    unsafe=self.unsafe)
                child_node.tag = 'tspan'
                # Retrieve the referenced node and get its flattened text
                # and remove the node children.
                child = child_tree.xml_tree
                child.text = flatten(child)
                child_element = cssselect2.ElementWrapper.from_xml_root(child)
            else:
                child_node = Node(
                    child_element, self.style, self.url_fetcher, parent=self,
                    unsafe=self.unsafe)
            child_preserve = child_node.get(space) == 'preserve'
            child_node.text = handle_white_spaces(child.text, child_preserve)
            child_node.children, trailing_space = child_node.text_children(
                child_element, trailing_space)
            trailing_space = child_node.text.endswith(' ')
            if original_rotate and 'rotate' not in child_node:
                pop_rotation(child_node, original_rotate, rotate)
            children.append(child_node)
            if child.tail:
                anonymous_etree = Element('{http://www.w3.org/2000/svg}tspan')
                anonymous = Node(
                    cssselect2.ElementWrapper.from_xml_root(anonymous_etree),
                    self.style, self.url_fetcher, parent=self,
                    unsafe=self.unsafe)
                anonymous.text = handle_white_spaces(child.tail, preserve)
                if original_rotate:
                    pop_rotation(anonymous, original_rotate, rotate)
                if trailing_space and not preserve:
                    anonymous.text = anonymous.text.lstrip(' ')
                if anonymous.text:
                    trailing_space = anonymous.text.endswith(' ')
                children.append(anonymous)

        if text_root and not children and not preserve:
            self.text = self.text.rstrip(' ')

        return children, trailing_space


class Tree(Node):
    """SVG tree."""
    def __new__(cls, **kwargs):
        tree_cache = kwargs.get('tree_cache')
        if tree_cache and kwargs.get('url'):
            parsed_url = parse_url(kwargs['url'])
            element_id = parsed_url.fragment
            parent = kwargs.get('parent')
            unsafe = kwargs.get('unsafe')
            if any(parsed_url[:-1]):
                url = urlunparse(parsed_url[:-1] + ('',))
            elif parent:
                url = parent.url
            else:
                url = None
            if url and (url, element_id) in tree_cache:
                cached_tree = tree_cache[(url, element_id)]
                new_tree = Node(
                    cached_tree.element, cached_tree.style,
                    cached_tree.url_fetcher, parent, unsafe=unsafe)
                new_tree.xml_tree = cached_tree.xml_tree
                new_tree.url = url
                new_tree.tag = cached_tree.tag
                new_tree.root = True
                return new_tree
        return super().__new__(cls)

    def __init__(self, **kwargs):
        """Create the Tree from SVG ``text``."""
        bytestring = kwargs.get('bytestring')
        file_obj = kwargs.get('file_obj')
        url = kwargs.get('url')
        unsafe = kwargs.get('unsafe')
        parent = kwargs.get('parent')
        parent_children = kwargs.get('parent_children')
        tree_cache = kwargs.get('tree_cache')
        element_id = None

        self.url_fetcher = kwargs.get('url_fetcher', fetch)

        if bytestring is not None:
            self.url = url
        elif file_obj is not None:
            bytestring = file_obj.read()
            self.url = getattr(file_obj, 'name', None)
            if self.url == '<stdin>':
                self.url = None
        elif url is not None:
            parent_url = parent.url if parent else None
            parsed_url = parse_url(url, parent_url)
            if parsed_url.fragment:
                self.url = urlunparse(parsed_url[:-1] + ('',))
                element_id = parsed_url.fragment
            else:
                self.url = parsed_url.geturl()
                element_id = None
            self.url = self.url or None
        else:
            raise TypeError(
                'No input. Use one of bytestring, file_obj or url.')
        if parent and self.url == parent.url:
            root_parent = parent
            while root_parent.parent:
                root_parent = root_parent.parent
            tree = root_parent.xml_tree
        else:
            if not bytestring:
                bytestring = self.fetch_url(
                    parse_url(self.url), 'image/svg+xml')
            if len(bytestring) >= 2 and bytestring[:2] == b'\x1f\x8b':
                bytestring = gzip.decompress(bytestring)
            tree = ElementTree.fromstring(
                bytestring, forbid_entities=not unsafe,
                forbid_external=not unsafe)
        self.xml_tree = tree
        root = cssselect2.ElementWrapper.from_xml_root(tree)
        style = parent.style if parent else css.parse_stylesheets(self, url)
        if element_id:
            for element in root.iter_subtree():
                if element.id == element_id:
                    root = element
                    self.xml_tree = element.etree_element
                    break
            else:
                raise TypeError(
                    'No tag with id="{}" found.'.format(element_id))
        super().__init__(
            root, style, self.url_fetcher, parent, parent_children, self.url,
            unsafe)
        self.root = True
        if tree_cache is not None and self.url:
            tree_cache[(self.url, self.get('id'))] = self


CASE_SENSITIVE_STYLE_METHODS = {
    'id': normalize_noop_style_declaration,
    'class': normalize_noop_style_declaration,
    'font-family': normalize_noop_style_declaration,
    'font': normalize_font_style_declaration,
    'clip-path': normalize_url_style_declaration,
    'color-profile': normalize_url_style_declaration,
    'cursor': normalize_url_style_declaration,
    'fill': normalize_url_style_declaration,
    'filter': normalize_url_style_declaration,
    'marker-start': normalize_url_style_declaration,
    'marker-mid': normalize_url_style_declaration,
    'marker-end': normalize_url_style_declaration,
    'mask': normalize_url_style_declaration,
    'stroke': normalize_url_style_declaration,
}
