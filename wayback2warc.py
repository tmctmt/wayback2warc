import argparse
import json
import os.path
import random
import re
import sys
import uuid
from collections import deque
from dataclasses import astuple, dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from multiprocessing.pool import ThreadPool
from operator import attrgetter

import httpx
from tqdm import tqdm
from warcio import StatusAndHeaders, WARCWriter


@dataclass(frozen=True, slots=True)
class Capture:
    url: str
    date: datetime
    statusline: str
    headers: list[tuple[str, str]]
    content: bytes


@dataclass(frozen=True, slots=True)
class CaptureMetadata:
    urlkey: str
    timestamp: str
    original: str
    mimetype: str
    statuscode: str
    digest: str
    length: str


class PooledClient:
    base_url = 'https://web.archive.org'
    timeout = httpx.Timeout(30, connect=5, read=30)
    max_retries = 30

    def __init__(self, proxy_list: list[str | None]):
        self.proxy_list = proxy_list
        self.session_pool: deque[httpx.Client] = deque()

    def request(self, method: str, url: str, retry: int = 1, **kwargs):
        try:
            session = self.session_pool.popleft()
        except IndexError:
            proxy = random.choice(self.proxy_list)
            session = httpx.Client(base_url=self.base_url, timeout=self.timeout, proxy=proxy)

        try:
            resp = session.request(method, url, **kwargs)
            # we're blocked if this header is not present
            # exempt error 400 from retries as the cause is usually url encoding issues on our part
            assert 'x-app-server' in resp.headers or resp.status_code == 400
        except:
            session.close()
            if retry > self.max_retries:
                raise
            return self.request(method, url, retry+1, **kwargs)
        else:
            self.session_pool.append(session)
            return resp
        
    def get_cdx_page_count(self, url: str):
        resp = self.request('GET', '/cdx/search/cdx', params={
            'url': url,
            'showNumPages': True
        })
        return int(resp.text)

    def get_cdx_page(self, url: str, page: int):
        resp = self.request('GET', '/cdx/search/cdx', params={
            'url': url,
            'page': page,
            'output': 'json'
        })
        rows = resp.json()
        return [CaptureMetadata(*row) for row in rows[1:]]
    
    def get_capture(self, url: str, timestamp: str):
        try:
            resp = self.request('GET', f'/web/{timestamp}id_/{url}')
        except:
            # .request already handles retries
            return

        # capture is not available
        if 'memento-datetime' not in resp.headers:
            return
        
        headers = [('content-length', f'{len(resp.content)}')]
        for header, value in resp.headers.multi_items():
            if header.startswith('x-archive-orig-'):
                header = header[15:]
            elif header == 'location':
                value = re.sub(r'^(?:https?://web\.archive\.org)?/web/\d{14}\w*/', '', value)
            elif header == 'content-type':
                pass
            else:
                continue
            if header not in ('content-length', 'content-encoding', 'transfer-encoding'):
                headers.append((header, value))

        try:
            original = resp.links['original']['url']
        except KeyError:
            original = url
        date = parsedate_to_datetime(resp.headers['memento-datetime'])
        statusline = f'HTTP/1.1 {resp.status_code} {resp.reason_phrase}'

        return Capture(original, date, statusline, headers, resp.content)


def main():
    p = argparse.ArgumentParser(
        description='WARC exporter for the Wayback Machine',
        epilog=(
            f'CaptureMetadata fields: {", ".join(CaptureMetadata.__annotations__)}\n\n'
            'Example usage:\n'
            '%(prog)s "*.example.org" --collapse "lambda m: m.digest"\n'
            '%(prog)s "twitter.com/elonmusk/status/*" --filter "lambda m: m.timestamp < \'2022\'"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('url',
                   help='*.example.org or example.org/*')
    p.add_argument('prefix', nargs='?', default='',
                   help='output path prefix')
    p.add_argument('-c', '--collapse', type=eval,
                   help='dedupe records based on returned key')
    p.add_argument('-f', '--filter', type=eval,
                   help='filter records based on returned boolean')
    p.add_argument('-m', '--meta', action='store_true',
                   help='dump cdx metadata into stdout')
    p.add_argument('-p', '--proxy',
                   help='url formatted proxy, can be a single value or a file')
    p.add_argument('-t', '--threads', type=int, default=2,
                   help='concurrent downloads (default: %(default)s)')
    p.add_argument('-w', '--warc-size', type=int, default=1024,
                   help='max size of produced warc files in MB (default: %(default)s)')
    p.add_argument('-s', '--page-start', type=int, default=0,
                   help='include cdx pages starting from this zero-based number (useful for splitting up large websites)')
    p.add_argument('-e', '--page-end', type=int, default=1e9,
                   help='include cdx pages up to this number')
    p.add_argument('-n', '--page-count', action='store_true',
                   help='print cdx page count and exit')
    args = p.parse_args()

    if args.collapse:
        CaptureMetadata.__eq__ = lambda self, other: args.collapse(self) == args.collapse(other)
        CaptureMetadata.__hash__ = lambda self: hash(args.collapse(self))

    if not args.proxy:
        proxy_list = [None]
    elif os.path.exists(args.proxy):
        with open(args.proxy) as file:
            proxy_list = file.read().splitlines()
    else:
        proxy_list = [args.proxy]

    client = PooledClient(proxy_list)
    pool = ThreadPool(args.threads)
    queue: set[CaptureMetadata] = set()
    
    pages = client.get_cdx_page_count(args.url)

    if args.page_count:
        print(pages)
        sys.exit()

    start = min(args.page_start, pages)
    end = min(args.page_end, pages)

    with tqdm(total=end - start, desc='listing cdx') as pbar:
        for chunk in pool.imap_unordered(
            lambda page: client.get_cdx_page(args.url, page),
            range(start, end)
        ):
            for meta in chunk:
                if meta in queue:
                    continue
                if args.filter and not args.filter(meta):
                    continue
                if int(meta.length) > 1024**2 * 100:
                    print(f'skipping large file: web.archive.org/web/{meta.timestamp}/{meta.original}', file=sys.stderr)
                    continue
                if args.meta:
                    pbar.write(json.dumps(astuple(meta)))
                queue.add(meta)
            pbar.set_postfix({'queued': len(queue)})
            pbar.update()

    if args.meta:
        sys.exit()

    queue = sorted(queue, key=attrgetter('timestamp'))
    
    with tqdm(total=len(queue), desc='downloading captures') as pbar:
        file = None
        skipped = 0

        for capture in pool.imap_unordered(
            lambda meta: client.get_capture(meta.original, meta.timestamp),
            queue
        ):
            if not capture:
                skipped += 1
                pbar.set_postfix({'skipped': skipped})
                pbar.update()
                continue
            
            if not file or file.tell() > 1024**2 * args.warc_size:
                if file:
                    file.close()
                file = open(f'{args.prefix}{uuid.uuid4()}.warc.gz', 'xb')
                writer = WARCWriter(file, gzip=True)

            record = writer.create_warc_record(
                uri=capture.url,
                record_type='response',
                payload=BytesIO(capture.content),
                warc_headers_dict={'WARC-Date': capture.date.isoformat().replace('+00:00', 'Z')},
                http_headers=StatusAndHeaders(capture.statusline, capture.headers)
            )
            deterministic_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f'{capture.url}|{capture.date.isoformat()}')
            record.rec_headers.replace_header('WARC-Record-ID', f'<urn:uuid:{deterministic_uuid}>')
            writer.write_record(record)
            pbar.update()

        if file:
            file.close()

    pool.terminate()


if __name__ == '__main__':
    main()
