"""Public HTML to plain text; no policy, network or model dependency."""
from html.parser import HTMLParser


class HTMLTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1
        if tag in {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'section'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'} and self.hidden:
            self.hidden -= 1
        if tag in {'p', 'div', 'li', 'section'} and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return '\n'.join(' '.join(line.split()) for line in ''.join(self.parts).splitlines() if line.strip())

