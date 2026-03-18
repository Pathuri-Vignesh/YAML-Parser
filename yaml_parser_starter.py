from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


class YamlParseError(Exception):
    pass


@dataclass
class Line:
    raw: str
    number: int
    indent: int
    content: str


INT_DEC_RE = re.compile(r'^[+-]?(?:0|[1-9][0-9]*)$')
INT_OCT_RE = re.compile(r'^[+-]?0o[0-7]+$', re.IGNORECASE)
INT_HEX_RE = re.compile(r'^[+-]?0x[0-9a-fA-F]+$')
FLOAT_RE = re.compile(
    r'^[+-]?(?:'
    r'(?:[0-9]+\.[0-9]*)|'
    r'(?:\.[0-9]+)|'
    r'(?:[0-9]+(?:[eE][+-]?[0-9]+))|'
    r'(?:[0-9]+\.[0-9]*[eE][+-]?[0-9]+)|'
    r'(?:\.[0-9]+[eE][+-]?[0-9]+)'
    r')$'
)
SPECIAL_FLOAT_RE = re.compile(r'^[+-]?\.(?:inf|Inf|INF|nan|NaN|NAN)$')


def strip_comment(s: str) -> str:
    """Remove comments starting with # unless inside quotes."""
    out = []
    in_single = False
    in_double = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            escaped = i > 0 and s[i - 1] == '\\'
            if not escaped:
                in_double = not in_double
            out.append(ch)
        elif ch == '#' and not in_single and not in_double:
            break
        else:
            out.append(ch)
        i += 1
    return ''.join(out).rstrip()


def preprocess(text: str) -> List[Line]:
    lines: List[Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if '\t' in raw:
            raise YamlParseError(f'Line {number}: tabs are not allowed for indentation')
        stripped = strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(' '))
        content = stripped[indent:]
        lines.append(Line(raw=raw, number=number, indent=indent, content=content))
    return lines


def split_key_value(s: str) -> Tuple[str, Optional[str]]:
    """Split on first colon that is not inside quotes/brackets/braces."""
    in_single = False
    in_double = False
    brace = 0
    bracket = 0
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            escaped = i > 0 and s[i - 1] == '\\'
            if not escaped:
                in_double = not in_double
        elif not in_single and not in_double:
            if ch == '{':
                brace += 1
            elif ch == '}':
                brace -= 1
            elif ch == '[':
                bracket += 1
            elif ch == ']':
                bracket -= 1
            elif ch == ':' and brace == 0 and bracket == 0:
                key = s[:i].strip()
                rest = s[i + 1:].strip()
                if not key:
                    raise YamlParseError('Missing key before colon')
                return key, rest if rest != '' else None
    raise YamlParseError(f'Expected key: value pair, got {s!r}')


def decode_double_quoted(s: str) -> str:
    body = s[1:-1]
    escapes = {
        'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\',
        '0': '\0', 'b': '\b', 'f': '\f', '/': '/'
    }
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise YamlParseError('Invalid escape at end of double-quoted string')
        esc = body[i]
        if esc in escapes:
            out.append(escapes[esc])
            i += 1
        elif esc == 'u':
            hexpart = body[i + 1:i + 5]
            if len(hexpart) != 4 or not re.fullmatch(r'[0-9a-fA-F]{4}', hexpart):
                raise YamlParseError('Invalid \\u escape')
            out.append(chr(int(hexpart, 16)))
            i += 5
        else:
            raise YamlParseError(f'Unsupported escape \\{esc}')
    return ''.join(out)


def parse_plain_scalar(token: str) -> Any:
    if token == '':
        return None
    if token in ('null', 'Null', 'NULL', '~'):
        return None
    if token in ('true', 'True', 'TRUE'):
        return True
    if token in ('false', 'False', 'FALSE'):
        return False
    if INT_OCT_RE.match(token):
        sign = -1 if token.startswith('-') else 1
        body = token[3:] if token[0] in '+-' else token[2:]
        return sign * int(body, 8)
    if INT_HEX_RE.match(token):
        sign = -1 if token.startswith('-') else 1
        body = token[3:] if token[0] in '+-' else token[2:]
        return sign * int(body, 16)
    if INT_DEC_RE.match(token):
        return int(token, 10)
    if SPECIAL_FLOAT_RE.match(token):
        low = token.lower()
        if 'nan' in low:
            return float('nan')
        return float('-inf' if low.startswith('-.') else 'inf')
    if FLOAT_RE.match(token):
        return float(token)
    return token


def split_flow_items(s: str, sep: str = ',') -> List[str]:
    items = []
    start = 0
    in_single = False
    in_double = False
    brace = 0
    bracket = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            escaped = i > 0 and s[i - 1] == '\\'
            if not escaped:
                in_double = not in_double
        elif not in_single and not in_double:
            if ch == '{':
                brace += 1
            elif ch == '}':
                brace -= 1
            elif ch == '[':
                bracket += 1
            elif ch == ']':
                bracket -= 1
            elif ch == sep and brace == 0 and bracket == 0:
                items.append(s[start:i].strip())
                start = i + 1
        i += 1
    tail = s[start:].strip()
    if tail:
        items.append(tail)
    return items


def parse_flow_array(token: str) -> list:
    inner = token[1:-1].strip()
    if not inner:
        return []
    return [parse_scalar(item) for item in split_flow_items(inner)]


def parse_flow_map(token: str) -> dict:
    inner = token[1:-1].strip()
    if not inner:
        return {}
    out = {}
    for item in split_flow_items(inner):
        key, value = split_key_value(item)
        out[key] = parse_scalar(value or '')
    return out


def parse_scalar(token: str) -> Any:
    token = token.strip()
    if token.startswith('"'):
        if not token.endswith('"') or len(token) < 2:
            raise YamlParseError('Unterminated double-quoted string')
        return decode_double_quoted(token)
    if token.startswith("'"):
        if not token.endswith("'") or len(token) < 2:
            raise YamlParseError('Unterminated single-quoted string')
        return token[1:-1].replace("''", "'")
    if token.startswith('['):
        if not token.endswith(']'):
            raise YamlParseError('Unterminated flow array')
        return parse_flow_array(token)
    if token.startswith('{'):
        if not token.endswith('}'):
            raise YamlParseError('Unterminated flow map')
        return parse_flow_map(token)
    return parse_plain_scalar(token)


class Parser:
    def __init__(self, lines: List[Line]):
        self.lines = lines
        self.i = 0

    def current(self) -> Optional[Line]:
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse(self) -> Any:
        if not self.lines:
            return {}
        first = self.current()
        assert first is not None
        if first.content.startswith('- '):
            return self.parse_block_sequence(first.indent)
        return self.parse_block_mapping(first.indent)

    def parse_block_mapping(self, indent: int) -> dict:
        result = {}
        while self.i < len(self.lines):
            line = self.current()
            assert line is not None
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlParseError(
                    f'Line {line.number}: unexpected indentation level {line.indent}, expected {indent}'
                )
            if line.content.startswith('- '):
                raise YamlParseError(f'Line {line.number}: sequence item found where mapping expected')

            key, value_text = split_key_value(line.content)
            self.i += 1

            if value_text in ('|', '>'):
                result[key] = self.parse_block_scalar(indent, folded=(value_text == '>'))
                continue

            if value_text is None:
                nxt = self.current()
                if nxt is None or nxt.indent <= indent:
                    result[key] = None
                elif nxt.content.startswith('- '):
                    result[key] = self.parse_block_sequence(nxt.indent)
                else:
                    result[key] = self.parse_block_mapping(nxt.indent)
            else:
                result[key] = parse_scalar(value_text)
        return result

    def parse_block_sequence(self, indent: int) -> list:
        result = []
        while self.i < len(self.lines):
            line = self.current()
            assert line is not None
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlParseError(
                    f'Line {line.number}: unexpected indentation level {line.indent}, expected {indent}'
                )
            if not line.content.startswith('- '):
                break

            item_text = line.content[2:].strip()
            self.i += 1

            if item_text == '':
                nxt = self.current()
                if nxt is None or nxt.indent <= indent:
                    result.append(None)
                elif nxt.content.startswith('- '):
                    result.append(self.parse_block_sequence(nxt.indent))
                else:
                    result.append(self.parse_block_mapping(nxt.indent))
            elif item_text in ('|', '>'):
                result.append(self.parse_block_scalar(indent, folded=(item_text == '>')))
            else:
                # support inline object item like: - name: Mittu
                try:
                    key, value_text = split_key_value(item_text)
                    obj = {key: None if value_text is None else parse_scalar(value_text or '')}
                    nxt = self.current()
                    if nxt is not None and nxt.indent > indent:
                        extra = self.parse_block_mapping(nxt.indent)
                        obj.update(extra)
                    result.append(obj)
                except YamlParseError:
                    result.append(parse_scalar(item_text))
        return result

    def parse_block_scalar(self, parent_indent: int, folded: bool) -> str:
        parts: List[str] = []
        first = self.current()
        if first is None or first.indent <= parent_indent:
            return ''
        block_indent = first.indent
        while self.i < len(self.lines):
            line = self.current()
            assert line is not None
            if line.indent < block_indent:
                break
            if line.indent == block_indent:
                parts.append(line.content)
            else:
                parts.append(' ' * (line.indent - block_indent) + line.content)
            self.i += 1
        if folded:
            return ' '.join(part.strip() for part in parts).strip()
        return '\n'.join(parts)


def parse_yaml(text: str) -> Any:
    lines = preprocess(text)
    parser = Parser(lines)
    return parser.parse()


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: python yaml_parser_starter.py <file.yaml>', file=sys.stderr)
        return 1
    path = sys.argv[1]
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = parse_yaml(f.read())
        print('Valid YAML')
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return 0
    except (OSError, YamlParseError, ValueError) as e:
        print(f'Invalid YAML: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
