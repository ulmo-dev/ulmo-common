from pyhis.waterml import common

WATERML_V1_1_NAMESPACE = "{http://www.cuahsi.org/waterML/1.1/}"


def parse_sites(content_io):
    """parses sites out of a waterml file; content_io should be a file-like object"""
    return common.parse_sites(content_io, WATERML_V1_1_NAMESPACE, site_info_name='sourceInfo')
