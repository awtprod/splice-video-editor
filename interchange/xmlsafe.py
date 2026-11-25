"""Parse untrusted XML timelines with expat while refusing every DTD feature.

FCPXML files carry ``<!DOCTYPE fcpxml>`` and nothing else in their DTD, so the parser accepts a
doctype without an internal subset or external identifier and refuses any entity declaration,
external entity reference, processing of parameter entities, or a document past the size and
element limits. Nothing here opens a file or a URL.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.parsers import expat

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_ELEMENTS = 200_000
MAX_DEPTH = 64


class UnsafeXML(ValueError):
    pass


def parse(data: bytes, *, max_elements: int = MAX_ELEMENTS) -> ET.Element:
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError('XML must be bytes')
    if len(data) > MAX_XML_BYTES:
        raise UnsafeXML('The timeline file is larger than 20 MB')
    parser = expat.ParserCreate()
    builder = ET.TreeBuilder()
    state = {'elements': 0, 'depth': 0}

    def refuse(*_args):
        raise UnsafeXML('The timeline file declares entities or an external DTD, which are refused')

    def doctype(_name, system_id, public_id, has_internal_subset):
        if system_id or public_id or has_internal_subset:
            refuse()

    def start(tag, attributes):
        state['elements'] += 1
        state['depth'] += 1
        if state['elements'] > max_elements:
            raise UnsafeXML(f'The timeline file has more than {max_elements} elements')
        if state['depth'] > MAX_DEPTH:
            raise UnsafeXML('The timeline file nests elements too deeply')
        builder.start(tag, attributes)

    def end(tag):
        state['depth'] -= 1
        builder.end(tag)

    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.NotationDeclHandler = refuse
    parser.ElementDeclHandler = refuse
    parser.AttlistDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True
    try:
        parser.Parse(bytes(data), True)
    except expat.ExpatError as error:
        raise ValueError(f'The timeline file is not well-formed XML: {error}') from None
    try:
        return builder.close()
    except AssertionError:  # TreeBuilder asserts when no element was closed
        raise ValueError('The timeline file has no XML root element') from None
