"""Existing speech markdown cleaning without global server logging."""
import re
from core.utils.text_utils import remove_emojis

punctuation_set = {
    "，",
    ",",  # Comma variants
    "。",
    ".",  # Period variants
    "！",
    "!",  # Exclamation variants
    "“",
    "”",
    '"',  # Quote variants
    "：",
    ":",  # Colon variants
    "-",
    "－",  # Hyphen variants
    "、",  # Enumeration separator
    "[",
    "]",  # Brackets
    "【",
    "】",  # Full-width brackets
    "~",  # Tilde
}

class MarkdownCleaner:
    """
    Preserve the existing Markdown-to-speech cleaning rules.
    """
    # Formula characters
    NORMAL_FORMULA_CHARS = re.compile(r'[a-zA-Z\\^_{}\+\-\(\)\[\]=]')

    @staticmethod
    def _replace_inline_dollar(m: re.Match) -> str:
        """
        只要捕获到完整的 "$...$":
          - 如果内部有典型Formula characters => 去掉两侧 $
          - 否则 (纯数字/货币等) => 保留 "$...$"
        """
        content = m.group(1)
        if MarkdownCleaner.NORMAL_FORMULA_CHARS.search(content):
            return content
        else:
            return m.group(0)

    @staticmethod
    def _replace_table_block(match: re.Match) -> str:
        """
        Convert a complete table block using the existing spoken labels.
        """
        block_text = match.group('table_block')
        lines = block_text.strip('\n').split('\n')

        parsed_table = []
        for line in lines:
            line_stripped = line.strip()
            if re.match(r'^\|\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?$', line_stripped):
                continue
            columns = [col.strip() for col in line_stripped.split('|') if col.strip() != '']
            if columns:
                parsed_table.append(columns)

        if not parsed_table:
            return ""

        headers = parsed_table[0]
        data_rows = parsed_table[1:] if len(parsed_table) > 1 else []

        lines_for_tts = []
        if len(parsed_table) == 1:
            # Single-row table
            only_line_str = ", ".join(parsed_table[0])
            lines_for_tts.append(f"单行表格：{only_line_str}")
        else:
            lines_for_tts.append(f"表头是：{', '.join(headers)}")
            for i, row in enumerate(data_rows, start=1):
                row_str_list = []
                for col_index, cell_val in enumerate(row):
                    if col_index < len(headers):
                        row_str_list.append(f"{headers[col_index]} = {cell_val}")
                    else:
                        row_str_list.append(cell_val)
                lines_for_tts.append(f"第 {i} 行：{', '.join(row_str_list)}")

        return "\n".join(lines_for_tts) + "\n"

    # Precompile substitutions in the existing order.
    # Callbacks are defined first so the substitutions can reference them.
    REGEXES = [
        (re.compile(r'```.*?```', re.DOTALL), ''),  # Code blocks
        (re.compile(r'^#+\s*', re.MULTILINE), ''),  # Headings
        (re.compile(r'(\*\*|__)(.*?)\1'), r'\2'),  # Bold
        (re.compile(r'(\*|_)(?=\S)(.*?)(?<=\S)\1'), r'\2'),  # Italics
        (re.compile(r'!\[.*?\]\(.*?\)'), ''),  # Images
        (re.compile(r'\[(.*?)\]\(.*?\)'), r'\1'),  # Links
        (re.compile(r'^\s*>+\s*', re.MULTILINE), ''),  # Blockquotes
        (
            re.compile(r'(?P<table_block>(?:^[^\n]*\|[^\n]*\n)+)', re.MULTILINE),
            _replace_table_block
        ),
        (re.compile(r'^\s*[*+-]\s*', re.MULTILINE), '- '),  # Lists
        (re.compile(r'\$\$.*?\$\$', re.DOTALL), ''),  # Block formulas
        (
            re.compile(r'(?<![A-Za-z0-9])\$([^\n$]+)\$(?![A-Za-z0-9])'),
            _replace_inline_dollar
        ),
        (re.compile(r'\n{2,}'), '\n'),  # Repeated blank lines
    ]

    @staticmethod
    def clean_markdown(text: str) -> str:
        """
        Apply ordered substitutions to remove or replace Markdown elements.
        """
        for regex, replacement in MarkdownCleaner.REGEXES:
            text = regex.sub(replacement, text)

        # Remove emoji.
        text = remove_emojis(text)

        # Preserve spacing for ASCII text and supported punctuation.
        if text and all((c.isascii() or c.isspace() or c in punctuation_set) for c in text):
            # Keep original spaces.
            return text

        return text.strip()
