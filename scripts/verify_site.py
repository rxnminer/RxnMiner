#!/usr/bin/env python3
"""Verify built local HTML links and assets under a GitHub Pages base path."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {'href', 'src', 'poster'} and value:
                self.links.append(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='/RxnMiner/')
    args = parser.parse_args()
    base = '/' + args.base.strip('/') + '/' if args.base.strip('/') else '/'
    root = Path(__file__).resolve().parents[1] / 'website/dist'
    errors, count = [], 0
    pages = list(root.rglob('*.html'))
    assert pages, 'Build the website first.'
    for page in pages:
        links = Links()
        links.feed(page.read_text(encoding='utf8'))
        current = base + page.relative_to(root).as_posix().removesuffix('index.html')
        for link in links.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or link.startswith('#'):
                continue
            resolved = unquote(urlsplit(urljoin(current, link)).path)
            if not resolved.startswith(base):
                errors.append(f'{page.relative_to(root)}: outside base: {link}')
                continue
            target = root / resolved[len(base):]
            if not target.is_file() and not (target / 'index.html').is_file():
                errors.append(f'{page.relative_to(root)}: missing: {link}')
            count += 1
    if errors:
        print('\n'.join(errors))
        return 1
    print(f'Verified {len(pages)} pages and {count} local links/assets under {base}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
