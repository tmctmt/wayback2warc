# wayback2warc
![PyPI - Version](https://img.shields.io/pypi/v/wayback2warc)

A CLI tool that exports captures from the Wayback Machine and packs them into WARC files.

```
usage: wayback2warc [-h] [-c COLLAPSE] [-f FILTER] [-m] [-p PROXY] [-t THREADS] [-w WARC_SIZE] url [prefix]

positional arguments:
  url                   *.example.org or example.org/*
  prefix                output path prefix

options:
  -h, --help            show this help message and exit
  -c, --collapse COLLAPSE
                        dedupe records based on returned key
  -f, --filter FILTER   filter records based on returned boolean
  -m, --meta            dump cdx metadata into stdout
  -p, --proxy PROXY     url formatted proxy, can be a single value or a file
  -t, --threads THREADS
                        concurrent downloads (default: 2)
  -w, --warc-size WARC_SIZE
                        max size of produced warc files in MB (default: 1024)
```

## Install
`pip install wayback2warc`

## Usage

Download all subdomains, all pages, and all captures for a domain:

`wayback2warc '*.example.org'`

Download all pages and all captures for a prefix:

`wayback2warc 'example.org/*'`

Download all captures for a single page:

`wayback2warc 'example.org'`

Download all pages but only a yearly capture of each:

`wayback2warc 'example.org/*' --collapse 'lambda m: (m.urlkey, m.timestamp[:4])'`

## Filtering and collapsing
This tool implements filtering/collapsing functionality on the client-side rather than relying on the CDX server to do so.
This is a necessity due to the way CDX paging works, but on the bright side it allows for much more granular queries.

Instead of a query language, lambda functions are used to narrow down captures for downloading.
A [CaptureMetadata](https://github.com/tmctmt/wayback2warc/blob/main/wayback2warc.py#L28) instance is passed to both functions, and the returned value is used as a key for collapsing, or as a boolean for filtering.

Collapse captures by URL excluding query:
`--collapse 'lambda m: m.urlkey.split("?")[0]'`

Filter captures before 2020 and exclude images:
`--filter 'lambda m: m.timestamp < "2020" and "image/" not in m.mimetype'`

## WARC records
The generated WARC files use per-record GZIP compression. Request records are not included. Response record payloads are always saved uncompressed with a `Content-Length` header, any `Content-Encoding` or `Transfer-Encoding` headers are stripped. Headers and status-lines are reconstructed on a best-effort basis, but the capitalization and order of headers may differ from the original capture. The HTTP version is always set to HTTP/1.1.
