"""Bit-exact model of Spark's CSV text writer and reader (univocity 2.9.1).

Spark's CSV text conventions are part of Ovid's output contract: the
disc writer, ``to_csv`` and — for legacy-parity pipelines
(``legacy_ovid``, :mod:`ovid2.legacy.csv_roundtrip`) — the side effects
of legacy Ovid writing a tab-delimited CSV and reading it back between
stages. On Spark those come from Spark's own CSV writer / reader. Every
other engine uses the pure functions here, which re-implement the exact
univocity code paths Spark configures — so the text a DuckDB / Snowpark
run produces is the text Spark produced.

* :func:`write_cell` — ``CsvWriter`` with Spark's writer options
  (``quote=""`` → ``\\0``, ``escape="\\\\"``, ``ignoreLeading/TrailingWhiteSpace``
  on, ``nullValue=None``, ``emptyValue='""'``, comment char ``#`` in the
  first column of a line). Verified against Spark 4.1 on ~40k fuzzed
  strings (``tests/unit/core/engines/test_csv_text.py``).
* :func:`parse_line` — ``CsvParser.parseSingleDelimiterRecord`` with
  Spark's reader options (``multiLine=false``, no whitespace trimming,
  ``STOP_AT_DELIMITER`` unescaped-quote handling, ``nullValue=""`` and
  ``emptyValue=""`` both read as NULL). A port of the Java source, line
  for line, including the fast quoted-string path and the recursive
  handling of content after a closing quote.
* :func:`split_lines` / :func:`is_blank_line` — record splitting and the
  "line is blank after Java ``trim``" skip.

Pure Python, no engine imports: the engines register these as UDFs.
"""

from __future__ import annotations

__all__ = [
    "SPARK_READ_ESCAPE",
    "is_blank_line",
    "parse_fields",
    "parse_line",
    "split_lines",
    "write_cell",
    "write_cell_sql",
    "write_line",
]

#: ``escape`` of the legacy ``cf.spark_read`` (``escape='"'``) and of a
#: plain ``spark.read.load(..., quote='"')`` (Spark's default backslash).
SPARK_READ_ESCAPE: dict[str, str] = {"spark_read": '"', "backslash_escape": "\\"}

_WRITER_QUOTE = "\x00"
_WRITER_ESCAPE = "\\"


def _ws(ch: str) -> bool:
    return ch <= " "


def _ws_nz(ch: str) -> bool:
    """univocity whitespace in the trim / skip paths: ``'\0' < ch <= ' '``."""
    return "\0" < ch <= " "


def write_cell(value: str | None, *, delimiter: str = "\t", line_start: bool = False) -> str | None:
    """Return the text Spark's CSV writer emits for one string cell.

    Args:
        value: The cell value (``None`` → ``None``: an empty field).
        delimiter: Field delimiter.
        line_start: ``True`` for the first column of a line (univocity
            quotes a value that begins with the comment char ``#`` there).

    Returns:
        The cell text (``'""'`` for an empty / all-whitespace string).
    """
    if value is None:
        return None
    stripped = value.lstrip("".join(chr(i) for i in range(33)))
    if stripped == "":
        return '""'
    quote = (line_start and value[:1] == "#") or any(
        c in stripped for c in (_WRITER_QUOTE, delimiter, "\n", "\r")
    )
    if quote:
        escaped = stripped.replace(_WRITER_ESCAPE, _WRITER_ESCAPE * 2).replace(
            _WRITER_QUOTE, _WRITER_ESCAPE + _WRITER_QUOTE
        )
        return _WRITER_QUOTE + _rstrip_ws(escaped) + _WRITER_QUOTE
    return _rstrip_ws(stripped)


def write_line(values: list[str | None], *, delimiter: str = "\t") -> str:
    """Render one row of string cells (``None`` → empty field)."""
    out = []
    for i, v in enumerate(values):
        cell = write_cell(v, delimiter=delimiter, line_start=i == 0)
        out.append("" if cell is None else cell)
    return delimiter.join(out)


def write_cell_sql(column_sql: str, *, delimiter: str = "\t", line_start: bool = False) -> str:
    """DuckDB SQL equal to :func:`write_cell` for a ``VARCHAR`` expression.

    Lets the DuckDB engine render the legacy writer text natively (in
    parallel) instead of through a per-cell Python UDF. The common case —
    a non-empty value with no char ``<= U+0020`` at either end and none of
    NUL / the delimiter / CR / LF (and no leading ``#`` at a line start) —
    is the value itself and is decided by one regex; everything else takes
    the full rule. Pinned against :func:`write_cell` by
    ``tests/unit/core/engines/test_csv_text_sql.py``.

    Args:
        column_sql: A SQL expression of type ``VARCHAR``.
        delimiter: Field delimiter (one char).
        line_start: First column of a line (``#`` forces quoting).

    Returns:
        A SQL expression of type ``VARCHAR`` (NULL stays NULL).
    """
    ws = " || ".join(f"chr({i})" for i in range(33))
    special = "\\x00\\n\\r" + _re_class_char(delimiter)
    plain_re = "'^[^\\x00-\\x20" + _re_class_char(delimiter) + ("#" if line_start else "") + "]"
    plain_re += (
        "(?:[^\\x00\\n\\r"
        + _re_class_char(delimiter)
        + "]*[^\\x00-\\x20"
        + _re_class_char(delimiter)
        + "])?$'"
    )
    s = f"ltrim({column_sql}, {ws})"
    needs_quote = f"regexp_matches({s}, '[{special}]')"
    if line_start:
        needs_quote += f" OR starts_with({column_sql}, '#')"
    escaped = f"replace(replace({s}, '\\', '\\\\'), chr(0), '\\' || chr(0))"
    return (
        f"CASE WHEN {column_sql} IS NULL THEN NULL "
        f"WHEN regexp_full_match({column_sql}, {plain_re}) THEN {column_sql} "
        f"WHEN {s} = '' THEN '\"\"' "
        f"WHEN {needs_quote} THEN chr(0) || rtrim({escaped}, {ws}) || chr(0) "
        f"ELSE rtrim({s}, {ws}) END"
    )


def _re_class_char(ch: str) -> str:
    """``ch`` escaped for an RE2 character class inside a SQL string literal."""
    if ch == "'":
        return "''"
    return f"\\x{{{ord(ch):x}}}"


def _rstrip_ws(text: str) -> str:
    j = len(text)
    while j > 0 and _ws(text[j - 1]):
        j -= 1
    return text[:j]


def split_lines(text: str) -> list[str]:
    """Split file text into records the way Spark's line reader does (``\\n`` / ``\\r\\n`` / ``\\r``)."""
    lines: list[str] = []
    i, n, start = 0, len(text), 0
    while i < n:
        ch = text[i]
        if ch == "\n" or ch == "\r":
            lines.append(text[start:i])
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            start = i + 1
        i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def is_blank_line(line: str) -> bool:
    """Spark drops a record whose text is empty after Java ``String.trim``."""
    return all(_ws(c) for c in line)


class _EOF(Exception):
    """Input exhausted (univocity ``EOFException``)."""


class _Malformed(Exception):
    """univocity ``TextParsingException`` (Spark PERMISSIVE: all-NULL row)."""


_NL = "\n"  # univocity's normalized line separator; never inside a Spark record
_COMMENT = "#"  # CsvFormat default comment char (processing disabled, still compared)


class _Parser:
    """Line-for-line port of univocity 2.9.1 ``CsvParser.parseLine`` (Spark reader settings).

    Spark hands each record to ``parseLine`` *without* a line terminator,
    so most fields end in the EOF path (``handleEOF`` /
    ``consumeValueOnEOF``). The NUL character doubles as univocity's
    "no previous char" sentinel and, with ``escape == quote``, as the
    ``escapeEscape`` stop char — both quirks are reproduced on purpose.
    """

    def __init__(
        self,
        line: str,
        delimiter: str,
        quote: str,
        escape: str,
        *,
        lead: bool = False,
        trail: bool = False,
    ) -> None:
        self.lead = lead  # ignoreLeadingWhiteSpace
        self.trail = trail  # ignoreTrailingWhiteSpace
        self.trim = False
        self.buf = line
        self.length = len(line)
        self.i = 0
        self.exhausted = False
        self.delim = delimiter
        self.quote = quote
        self.esc = escape
        # CsvFormat.getCharToEscapeQuoteEscaping: '\0' when quote == escape.
        self.esc_esc = "\0" if quote == escape else escape
        self.values: list[str | None] = []
        self.app: list[str] = []
        self.prev = "\0"
        self.ch = "\0"
        self.unescaped = False

    # -- input (AbstractCharInputReader) -----------------------------------

    def next_char(self) -> str:
        if self.exhausted:
            raise _EOF
        ch = self.buf[self.i]
        self.i += 1
        if self.i >= self.length:
            self.exhausted = True  # updateBuffer(): reload -> length -1, i 0
        return ch

    def _get_string(self, ch: str) -> str | None:
        """``getString(ch, delimiter, trim=ignoreTrailingWhiteSpace, nullValue="", -1)``."""
        if self.exhausted:
            return None
        i = self.i
        while ch != self.delim:
            if i >= self.length:
                return None
            ch = self.buf[i]
            i += 1
        pos, length = self.i - 1, i - self.i
        self.i = i - 1
        if self.trail:
            k = i - 2
            while k >= 0 and _ws_nz(self.buf[k]):
                length -= 1
                k -= 1
        out = "" if length <= 0 else self.buf[pos : pos + length]
        self.next_char()
        return out

    def _skip_whitespace(self) -> None:
        """``input.skipWhitespace(ch, delimiter, quote)`` (leading trim)."""
        while _ws_nz(self.ch) and self.ch not in (self.delim, _NL, self.quote):
            self.ch = self.next_char()

    def _get_quoted_string(self) -> str | None:
        """``getQuotedString`` (keepQuotes / keepEscape / trims all false)."""
        if self.exhausted:
            return None
        i = self.i
        while True:
            if i >= self.length:
                return None
            ch = self.buf[i]
            if ch == self.quote:
                if self.buf[i - 1] == self.esc:
                    return None
                if i + 1 < self.length and self.buf[i + 1] in (self.delim, _NL):
                    break
                return None
            if (
                ch == self.esc
                and i + 1 < self.length
                and self.buf[i + 1]
                in (
                    self.quote,
                    self.esc_esc,
                )
            ):
                return None
            i += 1
        out = self.buf[self.i : i]
        self.i = i + 1
        if self.i >= self.length:
            self.exhausted = True
        return out

    def _append_until(self, ch: str, *stops: str) -> str:
        while ch not in stops:
            self.app.append(ch)
            ch = self.next_char()
        return ch

    # -- output (ParserOutput) ------------------------------------------------

    def _value_parsed(self, text: str | None = None) -> None:
        if text is None:
            text = "".join(self.app)
            if self.trim:
                j = len(text)
                while j > 0 and _ws_nz(text[j - 1]):
                    j -= 1
                text = text[:j]
        self.app = []
        self.trim = False
        self.values.append(None if text == "" else text)

    def _empty_parsed(self) -> None:
        self.app = []
        self.values.append(None)

    # -- CsvParser ------------------------------------------------------------

    def parse(self) -> list[str | None]:
        try:
            self.ch = self.next_char()
            self._parse_record()
        except _EOF:
            self._handle_eof()
        return self.values

    def _parse_record(self) -> None:
        if self.lead and _ws_nz(self.ch):
            self._skip_whitespace()
        while self.ch != _NL:
            if self.lead and _ws_nz(self.ch):
                self._skip_whitespace()
            if self.ch in (self.delim, _NL):
                self._empty_parsed()
            else:
                self.unescaped = False
                self.prev = "\0"
                if self.ch == self.quote:
                    if not self.app:
                        value = self._get_quoted_string()
                        if value is not None:
                            self._value_parsed(value)
                            try:
                                self.ch = self.next_char()
                                if self.ch == self.delim:
                                    try:
                                        self.ch = self.next_char()
                                        if self.ch == _NL:
                                            self._empty_parsed()
                                    except _EOF:
                                        self._empty_parsed()
                                        return
                            except _EOF:
                                return
                            continue
                    self.trim = False  # trimQuotedTrailing
                    self._parse_quoted_value()
                    self._value_parsed()
                else:
                    value = self._get_string(self.ch) if not self.app else None
                    if value is not None:
                        self._value_parsed(value)
                        self.ch = self.delim  # input.getChar(): getString stops on it
                    else:
                        self.trim = self.trail
                        self.ch = self._append_until(self.ch, self.delim, _NL)
                        self._value_parsed()
            if self.ch != _NL:
                self.ch = self.next_char()
                if self.ch == _NL:
                    self._empty_parsed()

    def _process_quote_escape(self) -> None:
        ch, prev = self.ch, self.prev
        if ch == self.esc and prev == self.esc_esc and self.esc_esc != "\0":
            self.app.append(self.esc)
            self.ch = "\0"
        elif prev == self.esc:
            if ch == self.quote:
                self.app.append(self.quote)
                self.ch = "\0"
            else:
                self.app.append(prev)
        elif ch == self.quote and prev == self.quote:
            self.app.append(self.quote)
        elif prev == self.quote:
            self._handle_unescaped_quote_in_value()

    def _handle_unescaped_quote_in_value(self) -> None:
        self.app.append(self.quote)
        self.prev = self.ch
        self._parse_value_processing_escape()

    def _parse_value_processing_escape(self) -> None:
        while self.ch != self.delim and self.ch != _NL:
            if self.ch != self.quote and self.ch != self.esc:
                if self.prev == self.quote:
                    self._handle_unescaped_quote_in_value()
                    return
                self.app.append(self.ch)
            else:
                self._process_quote_escape()
            self.prev = self.ch
            self.ch = self.next_char()

    def _handle_unescaped_quote(self) -> None:
        self.unescaped = True
        self.app.append(self.quote)
        self.app.append(self.ch)
        self.prev = self.ch
        self._parse_quoted_value()

    def _parse_quoted_value(self) -> None:
        if self.prev != "\0":
            # STOP_AT_DELIMITER: the rest of the field, raw.
            self.app.insert(0, self.quote)
            self.ch = self.next_char()
            self.trim = self.trail
            self.ch = self._append_until(self.ch, self.delim, _NL)
            return
        self.ch = self.next_char()
        while True:
            if self.prev == self.quote and (
                _ws(self.ch) or self.ch == self.delim or self.ch == _NL
            ):
                break
            if self.ch != self.quote and self.ch != self.esc:
                if self.prev == self.quote:
                    self._handle_unescaped_quote()
                    return
                if self.prev == self.esc and self.esc != "\0":
                    self.app.append(self.esc)
                self.ch = self._append_until(self.ch, self.quote, self.esc, self.esc_esc)
                self.prev = self.ch
                self.ch = self.next_char()
            else:
                self._process_quote_escape()
                self.prev = self.ch
                self.ch = self.next_char()
                if self.unescaped and (self.ch == self.delim or self.ch == _NL):
                    return
        if self.ch != self.delim and self.ch != _NL and _ws(self.ch):
            spaces: list[str] = []
            while True:
                spaces.append(self.ch)
                self.ch = self.next_char()
                if self.ch == _NL:
                    return
                if not (_ws(self.ch) and self.ch != self.delim):
                    break
            if self.ch != self.delim:
                self.app.append(self.quote)
                self.app.extend(spaces)
                self.app.append(self.ch)
                self.prev = self.ch
                self._parse_quoted_value()
        if self.ch != self.delim and self.ch != _NL:
            raise _Malformed

    def _consume_value_on_eof(self) -> bool:
        if self.ch == self.quote:
            if self.prev == self.quote:
                return True
            if not self.unescaped:
                self.app.append(self.quote)
        out = self.prev != "\0" and self.ch != self.delim and self.ch != _NL and self.ch != _COMMENT
        self.ch = self.prev = "\0"
        return out

    def _handle_eof(self) -> None:
        consume = self._consume_value_on_eof()
        if self.values or consume:
            if self.app or consume:
                self._value_parsed()
            else:
                self._empty_parsed()  # currentParsedContentLength() > 0 (non-empty record)
        elif self.app:
            self._value_parsed()
        else:
            self._empty_parsed()


def parse_fields(
    line: str,
    *,
    delimiter: str = "\t",
    quote: str = '"',
    escape: str = '"',
    ignore_leading: bool = False,
    ignore_trailing: bool = False,
) -> list[str | None]:
    """Parse one record into exactly the fields univocity produced (no padding).

    Spark reads the header row this way: its field count is the frame width.
    A malformed record yields ``[]``.
    """
    try:
        return _Parser(
            line, delimiter, quote, escape, lead=ignore_leading, trail=ignore_trailing
        ).parse()
    except _Malformed:
        return []


def parse_line(
    line: str,
    *,
    width: int,
    delimiter: str = "\t",
    quote: str = '"',
    escape: str = '"',
    ignore_leading: bool = False,
    ignore_trailing: bool = False,
) -> list[str | None]:
    """Parse one record exactly like Spark's CSV reader (``multiLine=false``).

    Args:
        line: The record text (no line terminator).
        width: Number of columns (header width); short rows are
            NULL-padded, long rows truncated — Spark's PERMISSIVE mode.
        delimiter: Field delimiter.
        quote: Quote character.
        escape: Quote-escape character (``'"'`` for ``cf.spark_read``,
            ``'\\\\'`` for Spark's default).

    Returns:
        ``width`` values; ``None`` for empty fields.
    """
    try:
        values = _Parser(
            line, delimiter, quote, escape, lead=ignore_leading, trail=ignore_trailing
        ).parse()
    except _Malformed:
        return [None] * width
    if len(values) < width:
        values.extend([None] * (width - len(values)))
    return values[:width]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
