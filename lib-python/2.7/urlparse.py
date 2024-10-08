      
# -*- coding: utf-8 -*-
"""Parse (absolute and relative) URLs.

urlparse module is based upon the following RFC specifications.

RFC 3986 (STD66): "Uniform Resource Identifiers" by T. Berners-Lee, R. Fielding
and L.  Masinter, January 2005.

RFC 2732 : "Format for Literal IPv6 Addresses in URL's by R.Hinden, B.Carpenter
and L.Masinter, December 1999.

RFC 2396:  "Uniform Resource Identifiers (URI)": Generic Syntax by T.
Berners-Lee, R. Fielding, and L. Masinter, August 1998.

RFC 2368: "The mailto URL scheme", by P.Hoffman , L Masinter, J. Zawinski, July 1998.

RFC 1808: "Relative Uniform Resource Locators", by R. Fielding, UC Irvine, June
1995.

RFC 1738: "Uniform Resource Locators (URL)" by T. Berners-Lee, L. Masinter, M.
McCahill, December 1994

RFC 3986 is considered the current standard and any future changes to
urlparse module should conform with it.  The urlparse module is
currently not entirely compliant with this RFC due to defacto
scenarios for parsing, and for backward compatibility purposes, some
parsing quirks from older RFCs are retained. The testcases in
test_urlparse.py provides a good indicator of parsing behavior.

The WHATWG URL Parser spec should also be considered.  We are not compliant with
it either due to existing user code API behavior expectations (Hyrum's Law).
It serves as a useful guide when making changes.
"""

import re
import sys
import collections

__all__ = ["urlparse", "urlunparse", "urljoin", "urldefrag",
           "urlsplit", "urlunsplit", "urlencode", "parse_qs",
           "parse_qsl", "quote", "quote_plus", "quote_from_bytes",
           "unquote", "unquote_plus", "unquote_to_bytes"]

# A classification of schemes.
# The empty string classifies URLs with no scheme specified,
# being the default value returned by “urlsplit” and “urlparse”.

uses_relative = ['', 'ftp', 'http', 'gopher', 'nntp', 'imap',
                 'wais', 'file', 'https', 'shttp', 'mms',
                 'prospero', 'rtsp', 'rtspu', 'sftp',
                 'svn', 'svn+ssh', 'ws', 'wss']

uses_netloc = ['', 'ftp', 'http', 'gopher', 'nntp', 'telnet',
               'imap', 'wais', 'file', 'mms', 'https', 'shttp',
               'snews', 'prospero', 'rtsp', 'rtspu', 'rsync',
               'svn', 'svn+ssh', 'sftp', 'nfs', 'git', 'git+ssh',
               'ws', 'wss']

uses_params = ['', 'ftp', 'hdl', 'prospero', 'http', 'imap',
               'https', 'shttp', 'rtsp', 'rtspu', 'sip', 'sips',
               'mms', 'sftp', 'tel']

# These are not actually used anymore, but should stay for backwards
# compatibility.  (They are undocumented, but have a public-looking name.)

non_hierarchical = ['gopher', 'hdl', 'mailto', 'news',
                    'telnet', 'wais', 'imap', 'snews', 'sip', 'sips']

uses_query = ['', 'http', 'wais', 'imap', 'https', 'shttp', 'mms',
              'gopher', 'rtsp', 'rtspu', 'sip', 'sips']

uses_fragment = ['', 'ftp', 'hdl', 'http', 'gopher', 'news',
                 'nntp', 'wais', 'https', 'shttp', 'snews',
                 'file', 'prospero']

# Characters valid in scheme names
scheme_chars = ('abcdefghijklmnopqrstuvwxyz'
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                '0123456789'
                '+-.')

# XXX: Consider replacing with functools.lru_cache
MAX_CACHE_SIZE = 20
_parse_cache = {}


def clear_cache():
    """Clear the parse cache and the quoters cache."""
    _parse_cache.clear()
    _safe_quoters.clear()


class ResultMixin(object):
    """Shared methods for the parsed result objects."""

    @property
    def username(self):
        netloc = self.netloc
        if '@' in netloc:
            userinfo = netloc.rsplit('@', 1)[0]
            if ':' in userinfo:
                return userinfo.split(':', 1)[0]
            return userinfo
        return None

    @property
    def password(self):
        netloc = self.netloc
        if '@' in netloc:
            userinfo = netloc.rsplit('@', 1)[0]
            if ':' in userinfo:
                return userinfo.split(':', 1)[1]
        return None

    @property
    def hostname(self):
        netloc = self.netloc
        if netloc is None:
            return netloc
        if '[' in netloc and ']' in netloc or ':' in netloc:
            return self.netloc.split('@')[-1].split(':', 1)[0]
        elif '@' in netloc:
            return netloc.rsplit('@', 1)[1]
        return netloc

    @property
    def port(self):
        netloc = self.netloc
        if netloc is None:
            return netloc
        if '[' in netloc and ']' in netloc or ':' in netloc:
            port = self.netloc.split('@')[-1].split(':', 1)[1]
            if port:
                try:
                    return int(port, 10)
                except ValueError:
                    return None
        return None

# Result objects are more helpful than simple tuples
class DefragResult(ResultMixin, tuple):
    """DefragResult(url, fragment)

    A 2-tuple that contains the url without fragment identifier and the fragment
    identifier as a separate argument.
    """

    __slots__ = ()

    def __new__(cls, url, fragment):
        return tuple.__new__(cls, (url, fragment))

    def __repr__(self):
        return 'DefragResult(url=%r, fragment=%r)' % self

    url = property(lambda self: self[0], doc="The URL with no fragment identifier.")

    fragment = property(lambda self: self[1],
                            doc="Fragment identifier separated from URL, "
                                "that allows indirect identification of a "
                                "secondary resource by reference to a "
                                "primary resource and additional identifying "
                                "information.")


    def geturl(self):
        if self.fragment:
            return self.url + '#' + self.fragment
        else:
            return self.url

class SplitResult(ResultMixin, tuple):
    """SplitResult(scheme, netloc, path, query, fragment)

    A 5-tuple that contains the different components of a URL. Similar to
    ParseResult, but does not split params.
    """

    __slots__ = ()

    def __new__(cls, scheme, netloc, path, query, fragment):
        return tuple.__new__(cls, (scheme, netloc, path, query, fragment))

    def __repr__(self):
        return 'SplitResult(scheme=%r, netloc=%r, path=%r, query=%r, fragment=%r)' % self

    scheme = property(lambda self: self[0], doc="Specifies URL scheme for the request.")

    netloc = property(lambda self: self[1],
                        doc="Network location where the request is made to.")

    path = property(lambda self: self[2],
                    doc="The hierarchical path, such as the path to a file to download.")

    query = property(lambda self: self[3],
                    doc="The query component, that contains non-hierarchical data, "
                        "that along with data in path component, identifies a "
                        "resource in the scope of URI's scheme and network location.")

    fragment = property(lambda self: self[4],
                        doc="Fragment identifier, that allows indirect identification "
                            "of a secondary resource by reference to a primary resource "
                            "and additional identifying information.")


    def geturl(self):
        return urlunsplit(self)

class ParseResult(ResultMixin, tuple):
    """ParseResult(scheme, netloc, path, params, query, fragment)

    A 6-tuple that contains components of a parsed URL.
    """

    __slots__ = ()

    def __new__(cls, scheme, netloc, path, params, query, fragment):
        return tuple.__new__(cls, (scheme, netloc, path, params, query, fragment))

    def __repr__(self):
        return 'ParseResult(scheme=%r, netloc=%r, path=%r, params=%r, query=%r, fragment=%r)' % self

    scheme = property(lambda self: self[0], doc="Specifies URL scheme for the request.")

    netloc = property(lambda self: self[1],
                        doc="Network location where the request is made to.")

    path = property(lambda self: self[2],
                    doc="The hierarchical path, such as the path to a file to download.")

    params = property(lambda self: self[3],
                        doc="Parameters for last path element used to dereference the URI "
                            "in order to provide access to perform some operation on the resource.")

    query = property(lambda self: self[4],
                    doc="The query component, that contains non-hierarchical data, "
                        "that along with data in path component, identifies a "
                        "resource in the scope of URI's scheme and network location.")

    fragment = property(lambda self: self[5],
                        doc="Fragment identifier, that allows indirect identification "
                            "of a secondary resource by reference to a primary resource "
                            "and additional identifying information.")


    def geturl(self):
        return urlunparse(self)

def urlparse(url, scheme='', allow_fragments=True):
    """Parse a URL into 6 components:
    <scheme>://<netloc>/<path>;<params>?<query>#<fragment>

    The result is a named 6-tuple with fields corresponding to the
    above. It is either a ParseResult or ParseResultBytes object,
    depending on the type of the url parameter.

    The username, password, hostname, and port sub-components of netloc
    can also be accessed as attributes of the returned object.

    The scheme argument provides the default value of the scheme
    component when no scheme is found in url.

    If allow_fragments is False, no attempt is made to separate the
    fragment component from the previous component, which can be either
    path or query.

    Note that % escapes are not expanded.
    """
    url, scheme, _coerce_result = (url, scheme, lambda x: x)
    splitresult = urlsplit(url, scheme, allow_fragments)
    scheme, netloc, url, query, fragment = splitresult
    if scheme in uses_params and ';' in url:
        url, params = _splitparams(url)
    else:
        params = ''
    result = ParseResult(scheme, netloc, url, params, query, fragment)
    return _coerce_result(result)


def _splitparams(url):
    if '/' in url:
        i = url.find(';', url.rfind('/'))
        if i < 0:
            return url, ''
    else:
        i = url.find(';')
    return url[:i], url[i + 1:]


def _splitnetloc(url, start=0):
    delim = len(url)  # position of end of domain part of url, default is end
    for c in '/?#':  # look for delimiters; the order is NOT important
        wdelim = url.find(c, start)  # find first of this delim
        if wdelim >= 0:  # if found
            delim = min(delim, wdelim)  # use earliest delim position
    return url[start:delim], url[delim:]  # return (domain, rest)


def urlsplit(url, scheme='', allow_fragments=True):
    """Parse a URL into 5 components:
    <scheme>://<netloc>/<path>?<query>#<fragment>

    The result is a named 5-tuple with fields corresponding to the
    above. It is either a SplitResult or SplitResultBytes object,
    depending on the type of the url parameter.

    The username, password, hostname, and port sub-components of netloc
    can also be accessed as attributes of the returned object.

    The scheme argument provides the default value of the scheme
    component when no scheme is found in url.

    If allow_fragments is False, no attempt is made to separate the
    fragment component from the previous component, which can be either
    path or query.

    Note that % escapes are not expanded.
    """
    url, scheme, _coerce_result = (url, scheme, lambda x: x)
    allow_fragments = bool(allow_fragments)
    key = url, scheme, allow_fragments, type(url), type(scheme)
    cached = _parse_cache.get(key, None)
    if cached:
        return _coerce_result(cached)
    if len(_parse_cache) >= MAX_CACHE_SIZE: # avoid runaway growth
        clear_cache()
    netloc = query = fragment = ''
    i = url.find(':')
    if i > 0:
        if url[:i] == 'http': # optimize the common case
            scheme = url[:i].lower()
            url = url[i+1:]
            if url[:2] == '//':
                netloc, url = _splitnetloc(url, 2)
                if '[' in netloc and ']' not in netloc or ']' in netloc and '[' not in netloc:
                    raise ValueError("Invalid IPv6 URL")
            if allow_fragments and '#' in url:
                url, fragment = url.split('#', 1)
            if '?' in url:
                url, query = url.split('?', 1)
            v = SplitResult(scheme, netloc, url, query, fragment)
            _parse_cache[key] = v
            return _coerce_result(v)
        for c in url[:i]:
            if c not in scheme_chars:
                break
        else:
            # make sure "url" is not and empty string so that the
            # "url[:2] =="// test doesn't raise an exception
            scheme, url = url[:i].lower(), url[i + 1:]
    if url[:2] == '//':
        netloc, url = _splitnetloc(url, 2)
        if '[' in netloc and ']' not in netloc or ']' in netloc and '[' not in netloc:
            raise ValueError("Invalid IPv6 URL")
    if allow_fragments and '#' in url:
        url, fragment = url.split('#', 1)
    if '?' in url:
        url, query = url.split('?', 1)
    v = SplitResult(scheme, netloc, url, query, fragment)
    _parse_cache[key] = v
    return _coerce_result(v)


def urlunparse(components):
    """Put a parsed URL back together again.  This may result in a
    slightly different, but equivalent URL, if the URL that was parsed
    originally had redundant delimiters, e.g. a ? with an empty query
    (the draft states that these are equivalent)."""
    scheme, netloc, url, params, query, fragment, _coerce_result = (
        components + (lambda x: x,))
    if params:
        url = "%s;%s" % (url, params)
    return _coerce_result(urlunsplit((scheme, netloc, url, query, fragment)))


def urlunsplit(components):
    """Combine the elements of a tuple as returned by urlsplit() into a
    complete URL as a string. The data argument can be any five-item iterable.
    This may result in a slightly different, but equivalent URL, if the URL that
    was parsed originally had unnecessary delimiters (for example, a ? with an
    empty query; the RFC states that these are equivalent)."""
    scheme, netloc, url, query, fragment, _coerce_result = (
        components + (lambda x: x,))
    if netloc or (scheme and scheme in uses_netloc and url[:2] != '//'):
        if url and url[:1] != '/':
            url = '/' + url
        url = '//' + (netloc or '') + url
    if scheme:
        url = scheme + ':' + url
    if query:
        url = url + '?' + query
    if fragment:
        url = url + '#' + fragment
    return _coerce_result(url)


def urljoin(base, url, allow_fragments=True):
    """Join a base URL and a possibly relative URL to form an absolute
    interpretation of the latter."""
    if not base:
        return url
    if not url:
        return base

    base, url, _coerce_result = (base, url, lambda x: x)
    bscheme, bnetloc, bpath, bparams, bquery, bfragment = \
        urlparse(base, '', allow_fragments)
    scheme, netloc, path, params, query, fragment = \
        urlparse(url, bscheme, allow_fragments)

    if scheme != bscheme or scheme not in uses_relative:
        return _coerce_result(url)
    if scheme in uses_netloc:
        if netloc:
            return _coerce_result(urlunparse((scheme, netloc, path,
                                              params, query, fragment)))
        netloc = bnetloc

    if not path and not params:
        path = bpath
        params = bparams
        if not query:
            query = bquery
        return _coerce_result(urlunparse((scheme, netloc, path,
                                          params, query, fragment)))

    base_parts = bpath.split('/')
    if base_parts[-1] != '':
        # the last item is not a directory, so will not be taken into account
        # in resolving the relative path
        del base_parts[-1]

    # for rfc3986, ignore all base path should the first character be root.
    if path[:1] == '/':
        segments = path.split('/')
    else:
        segments = base_parts + path.split('/')
        # filter out elements that would cause redundant slashes on re-joining
        # the resolved_path
        segments[1:-1] = filter(None, segments[1:-1])

    resolved_path = []

    for seg in segments:
        if seg == '..':
            try:
                resolved_path.pop()
            except IndexError:
                # ignore any .. segments that would otherwise cause an IndexError
                # when popped from resolved_path if resolving for rfc3986
                pass
        elif seg == '.':
            continue
        else:
            resolved_path.append(seg)

    if segments[-1] in ('.', '..'):
        # do some post-processing here. if the last segment was a relative dir,
        # then we need to append the trailing '/'
        resolved_path.append('')

    return _coerce_result(urlunparse((scheme, netloc, '/'.join(
        resolved_path) or '/', params, query, fragment)))


def urldefrag(url):
    """Removes any existing fragment from URL.

    Returns a tuple of the defragmented URL and the fragment.  If
    the URL contained no fragments, the second element is the
    empty string.
    """
    url, _coerce_result = (url, lambda x: x)
    if '#' in url:
        s, n, p, a, q, frag = urlparse(url)
        defrag = urlunparse((s, n, p, a, q, ''))
    else:
        frag = ''
        defrag = url
    return _coerce_result(DefragResult(defrag, frag))


_hexdig = '0123456789ABCDEFabcdef'
_hextobyte = dict(((a + b).decode('ascii'), chr(int(a + b, 16)))
                   for a in _hexdig for b in _hexdig)


def unquote_to_bytes(string):
    """unquote_to_bytes('abc%20def') -> 'abc def'."""
    # Note: strings are encoded as UTF-8. This is only an issue if it contains
    # unescaped non-ASCII characters, which URIs should not.
    if isinstance(string, unicode):
        string = string.encode('utf-8')
    bits = string.split('%')
    if len(bits) == 1:
        return string
    res = [bits[0]]
    append = res.append
    for item in bits[1:]:
        try:
            append(_hextobyte[item[:2]])
            append(item[2:])
        except KeyError:
            append('%')
            append(item)
    return "".join(res)

_asciire = re.compile('([\x00-\x7f]+)')

def unquote(string, encoding='utf-8', errors='replace'):
    """Replace %xx escapes by their single-character equivalent. The optional
    encoding and errors parameters specify how to decode percent-encoded
    sequences into Unicode characters, as accepted by the bytes.decode()
    method.
    By default, percent-encoded sequences are decoded with UTF-8, and invalid
    sequences are replaced by a placeholder character.

    unquote('abc%20def') -> 'abc def'.
    """
    if isinstance(string, str):
        string = string.decode('ascii')
    bits = _asciire.split(string)
    res = [bits[0]]
    append = res.append
    for i in range(1, len(bits), 2):
        append(unquote_to_bytes(bits[i]).decode(encoding, errors))
        append(bits[i + 1])
    return u''.join(res)


def parse_qs(qs, keep_blank_values=False, strict_parsing=False,
             encoding='utf-8', errors='replace'):
    """Parse a query given as a string argument.

        Arguments:

        qs: percent-encoded query string to be parsed

        keep_blank_values: flag indicating whether blank values in
            percent-encoded queries should be treated as blank strings.
            A true value indicates that blanks should be retained as
            blank strings.  The default false value indicates that
            blank values are to be ignored and treated as if they were
            not included.

        strict_parsing: flag indicating what to do with parsing errors.
            If false (the default), errors are silently ignored.
            If true, errors raise a ValueError exception.

        encoding and errors: specify how to decode percent-encoded sequences
            into Unicode characters, as accepted by the bytes.decode() method.

        Returns a dictionary.
    """
    parsed_result = {}
    for name, value in parse_qsl(qs, keep_blank_values, strict_parsing,
                                 encoding=encoding, errors=errors):
        if name in parsed_result:
            parsed_result[name].append(value)
        else:
            parsed_result[name] = [value]
    return parsed_result


def parse_qsl(qs, keep_blank_values=False, strict_parsing=False,
              encoding='utf-8', errors='replace'):
    """Parse a query given as a string argument.

        Arguments:

        qs: percent-encoded query string to be parsed

        keep_blank_values: flag indicating whether blank values in
            percent-encoded queries should be treated as blank strings.
            A true value indicates that blanks should be retained as blank
            strings.  The default false value indicates that blank values
            are to be ignored and treated as if they were  not included.

        strict_parsing: flag indicating what to do with parsing errors. If
            false (the default), errors are silently ignored. If true,
            errors raise a ValueError exception.

        encoding and errors: specify how to decode percent-encoded sequences
            into Unicode characters, as accepted by the bytes.decode() method.

        Returns a list, as G-d intended.
    """
    qs, _coerce_result = (qs, lambda x: x)
    pairs = [s2 for s1 in qs.split('&') for s2 in s1.split(';')]
    r = []
    for name_value in pairs:
        if not name_value and not strict_parsing:
            continue
        nv = name_value.split('=', 1)
        if len(nv) != 2:
            if strict_parsing:
                raise ValueError("bad query field: %r" % (name_value,))
            # Handle case of a control-name with no equal sign
            if keep_blank_values:
                nv.append('')
            else:
                continue
        if len(nv[1]) or keep_blank_values:
            name = nv[0].replace('+', ' ')
            name = unquote(name, encoding=encoding, errors=errors)
            name = _coerce_result(name)
            value = nv[1].replace('+', ' ')
            value = unquote(value, encoding=encoding, errors=errors)
            value = _coerce_result(value)
            r.append((name, value))
    return r


def unquote_plus(string, encoding='utf-8', errors='replace'):
    """Like unquote(), but also replace plus signs by spaces, as required for
    unquoting HTML form values.

    unquote_plus('%7e/abc+def') -> '~/abc def'
    """
    string = string.replace('+', ' ')
    return unquote(string, encoding, errors)


_ALWAYS_SAFE = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                         'abcdefghijklmnopqrstuvwxyz'
                         '0123456789'
                         '_.-~')
_ALWAYS_SAFE_BYTES = ''.join(c.encode('ascii') for c in _ALWAYS_SAFE) 
_safe_quoters = {}


class Quoter(collections.defaultdict):
    """A mapping from bytes (in range(0,256)) to strings.

    String values are percent-encoded byte values, unless the key < 128, and
    in the "safe" set (either the specified safe set, or default set).
    """
    # Keeps a cache internally, using defaultdict, for efficiency (lookups
    # of cached keys don't call Python code at all).

    def __init__(self, safe):
        """safe: bytes object."""
        self.safe = _ALWAYS_SAFE.union(safe)

    def __repr__(self):
        # Without this, will just display as a defaultdict
        return "<%s %r>" % (self.__class__.__name__, dict(self))

    def __missing__(self, b):
        # Handle a cache miss. Store quoted string in cache and return.
        if b in self.safe:
            res = b 
        else:
            res = '%%%02X' % ord(b[0])  # Use ord(b[0]) to get the byte value
        self[b] = res
        return res


def quote(string, safe='/', encoding=None, errors=None):
    """quote('abc def') -> 'abc%20def'

    Each part of a URL, e.g. the path info, the query, etc., has a
    different set of reserved characters that must be quoted. The
    quote function offers a cautious (not minimal) way to quote a
    string for most of these parts.

    RFC 3986 Uniform Resource Identifier (URI): Generic Syntax lists
    the following (un)reserved characters.

    unreserved    = ALPHA / DIGIT / "-" / "." / "_" / "~"
    reserved      = gen-delims / sub-delims
    gen-delims    = ":" / "/" / "?" / "#" / "[" / "]" / "@"
    sub-delims    = "!" / "$" / "&" / "'" / "(" / ")"
                  / "*" / "+" / "," / ";" / "="

    Each of the reserved characters is reserved in some component of a URL,
    but not necessarily in all of them.

    The quote function %-escapes all characters that are neither in the
    unreserved chars ("always safe") nor the additional chars set via the
    safe arg.

    The default for the safe arg is '/'. The character is reserved, but in
    typical usage the quote function is being called on a path where the
    existing slash characters are to be preserved.

    Python 3.7 updates from using RFC 2396 to RFC 3986 to quote URL strings.
    Now, "~" is included in the set of unreserved characters.

    string and safe may be either str or bytes objects. encoding and errors
    must not be specified if string is a bytes object.

    The optional encoding and errors parameters specify how to deal with
    non-ASCII characters, as accepted by the str.encode method.
    By default, encoding='utf-8' (characters are encoded with UTF-8), and
    errors='strict' (unsupported characters raise a UnicodeEncodeError).
    """
    if isinstance(string, unicode):
        if not string:
            return string
        if encoding is None:
            encoding = 'utf-8'
        if errors is None:
            errors = 'strict'
        string = string.encode(encoding, errors)
    else:
        if encoding is not None:
            raise TypeError("quote() doesn't support 'encoding' for bytes")
        if errors is not None:
            raise TypeError("quote() doesn't support 'errors' for bytes")
    return quote_from_bytes(string, safe)


def quote_plus(string, safe='', encoding=None, errors=None):
    """Like quote(), but also replace ' ' with '+', as required for quoting
    HTML form values. Plus signs in the original string are escaped unless
    they are included in safe. It also does not have safe default to '/'.
    """
    # Check if ' ' in string, where string may either be a str or bytes.  If
    # there are no spaces, the regular quote will produce the right answer.
    if ((isinstance(string, str) and ' ' not in string) or
        (isinstance(string, unicode) and u' ' not in string) or
        (isinstance(string, bytes) and b' ' not in string)):
        return quote(string, safe, encoding, errors)
    if isinstance(safe, unicode):
        space = u' '
    elif isinstance(safe, str):
        space = ' '
    else:
        space = b' '
    string = quote(string, safe + space, encoding, errors)
    return string.replace(' ', '+')


def quote_from_bytes(bs, safe='/'):
    """Like quote(), but accepts a bytes object rather than a str, and does
    not perform string-to-bytes encoding.  It always returns an ASCII string.
    quote_from_bytes(b'abc def\x3f') -> 'abc%20def%3f'
    """
    if not isinstance(bs, (bytes, bytearray)):
        raise TypeError("quote_from_bytes() expected bytes")
    if not bs:
        return ''
    if isinstance(safe, unicode):
        # Normalize 'safe' by converting to bytes and removing non-ASCII chars
        safe = safe.encode('ascii', 'ignore')
    else:
        safe = bytes([c for c in safe if c < 128])
    if not bs.rstrip(_ALWAYS_SAFE_BYTES + safe):
        return bs.decode()
    try:
        quoter = _safe_quoters[safe]
    except KeyError:
        _safe_quoters[safe] = quoter = Quoter(safe).__getitem__
    return ''.join([quoter(char) for char in bs])


def urlencode(query, doseq=False, safe='', encoding=None, errors=None,
              quote_via=quote_plus):
    """Encode a dict or sequence of two-element tuples into a URL query string.

    If any values in the query arg are sequences and doseq is true, each
    sequence element is converted to a separate parameter.

    If the query arg is a sequence of two-element tuples, the order of the
    parameters in the output will match the order of parameters in the
    input.

    The components of a query arg may each be either a string or a bytes type.

    The safe, encoding, and errors parameters are passed down to the function
    specified by quote_via (encoding and errors only if a component is a str).
    """

    if hasattr(query, "items"):
        query = query.items()
    else:
        # It's a bother at times that strings and string-like objects are
        # sequences.
        try:
            # non-sequence items should not work with len()
            # non-empty strings will fail this
            if len(query) and not isinstance(query[0], tuple):
                raise TypeError
            # Zero-length sequences of all types will get here and succeed,
            # but that's a minor nit.  Since the original implementation
            # allowed empty dicts that type of behavior probably should be
            # preserved for consistency
        except TypeError:
            ty, va, tb = sys.exc_info()
            raise TypeError("not a valid non-string sequence "
                            "or mapping object").with_traceback(tb)

    l = []
    if not doseq:
        for k, v in query:
            if isinstance(k, bytes):
                k = quote_via(k, safe)
            else:
                k = quote_via(str(k), safe, encoding, errors)

            if isinstance(v, bytes):
                v = quote_via(v, safe)
            else:
                v = quote_via(str(v), safe, encoding, errors)
            l.append(k + '=' + v)
    else:
        for k, v in query:
            if isinstance(k, bytes):
                k = quote_via(k, safe)
            else:
                k = quote_via(str(k), safe, encoding, errors)

            if isinstance(v, bytes):
                v = quote_via(v, safe)
                l.append(k + '=' + v)
            elif isinstance(v, (str, unicode)):
                v = quote_via(v, safe, encoding, errors)
                l.append(k + '=' + v)
            else:
                try:
                    # Is this a sufficient test for sequence-ness?
                    x = len(v)
                except TypeError:
                    # not a sequence
                    v = quote_via(str(v), safe, encoding, errors)
                    l.append(k + '=' + v)
                else:
                    # loop over the sequence
                    for elt in v:
                        if isinstance(elt, bytes):
                            elt = quote_via(elt, safe)
                        else:
                            elt = quote_via(str(elt), safe, encoding, errors)
                        l.append(k + '=' + elt)
    return '&'.join(l)


def to_bytes(url):
    """to_bytes(u"URL") --> 'URL'."""
    # Most URL schemes require ASCII. If that changes, the conversion
    # can be relaxed.
    # XXX get rid of to_bytes()
    if isinstance(url, unicode):
        try:
            url = url.encode("ASCII").decode()
        except UnicodeError:
            raise UnicodeError("URL " + repr(url) +
                               " contains non-ASCII characters")
    return url


def unwrap(url):
    """Transform a string like '<URL:scheme://host/path>' into 'scheme://host/path'.

    The string is returned unchanged if it's not a wrapped URL.
    """
    url = str(url).strip()
    if url[:1] == '<' and url[-1:] == '>':
        url = url[1:-1].strip()
    if url[:4] == 'URL:':
        url = url[4:].strip()
    return url


def splittype(url):
    """splittype('type:opaquestring') --> 'type', 'opaquestring'."""
    if ':' in url:
        i = url.find(':')
        return url[:i], url[i+1:]
    return None, url


def splithost(url):
    """splithost('//host[:port]/path') --> 'host[:port]', '/path'."""
    if url[0] == '/' and url[1] == '/':
        i = url.find('/', 2)
        if i < 0:
            return url[2:], ''
        return url[2:i], url[i:]
    return None, url

def splituser(host):
    """splituser('user[:passwd]@host[:port]') --> 'user[:passwd]', 'host[:port]'."""
    if '@' in host:
        i = host.find('@')
        return host[:i], host[i+1:]
    return None, host


def splitpasswd(user):
    """splitpasswd('user:passwd') -> 'user', 'passwd'."""
    if ':' in user:
        i = user.find(':')
        return user[:i], user[i+1:]
    return user, None


def splitport(host):
    """splitport('host:port') --> 'host', 'port'."""
    if host and host[-1] == ']':
        return None, None
    if ':' in host:
        i = host.rfind(':')
        if i > host.find('['):
            return host, None
        if i > host.find(']') and host.find(']') != -1:
            return host, None
        port = host[i+1:]
        if port and port.isdigit():
            port = int(port, 10)
        return host[:i], port
    return host, None


def splitnport(host, defport=-1):
    """Split host and port, returning numeric port.
    Return given default port if no ':' found; defaults to -1.
    Return numerical port if a valid number are found after ':'.
    Return None if ':' but not a valid number."""
    host, port = splitport(host)
    if port is None:
        return host, defport
    try:
        nport = int(port)
    except ValueError:
        nport = None
    return host, nport


def splitquery(url):
    """splitquery('/path?query') --> '/path', 'query'."""
    if '?' in url:
        i = url.find('?')
        return url[:i], url[i+1:]
    return url, None


def splittag(url):
    """splittag('/path#tag') --> '/path', 'tag'."""
    if '#' in url:
        i = url.find('#')
        return url[:i], url[i+1:]
    return url, None


def splitattr(url):
    """splitattr('/path;attr1=value1;attr2=value2;...') ->
        '/path', ['attr1=value1', 'attr2=value2', ...]."""
    words = url.split(';')
    return words[0], words[1:]


def splitvalue(attr):
    """splitvalue('attr=value') --> 'attr', 'value'."""
    if '=' in attr:
        i = attr.find('=')
        return attr[:i], attr[i+1:]
    return attr, None
