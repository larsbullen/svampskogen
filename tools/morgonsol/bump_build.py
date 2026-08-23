#!/usr/bin/env python3
"""Morgonsol — bump the build number across every file that has to agree.

GitHub Pages serves everything with `cache-control: max-age=600`. That applies
to sw.js too, so without this a phone can sit on a ten-minute-old version of the
app and there is no way to tell from looking at it. Versioning the asset URLs
makes a deploy take effect immediately instead of eventually.

Run this before every commit that changes the app:
    python3 tools/morgonsol/bump_build.py && git add -A && git commit && git push
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))


def read(p):
    with open(os.path.join(ROOT, p)) as f:
        return f.read()


def write(p, s):
    with open(os.path.join(ROOT, p), "w") as f:
        f.write(s)


def main():
    js = read("morgonsol.js")
    m = re.search(r"^const BUILD = (\d+);", js, flags=re.M)
    if not m:
        sys.exit("no `const BUILD = N;` line in morgonsol.js")
    old = int(m.group(1))
    new = int(sys.argv[1]) if len(sys.argv) > 1 else old + 1

    write("morgonsol.js", re.sub(r"^const BUILD = \d+;", "const BUILD = %d;" % new,
                                 js, count=1, flags=re.M))

    html = read("morgonsol.html")
    html = re.sub(r'href="morgonsol\.css\?v=\d+"', 'href="morgonsol.css?v=%d"' % new, html)
    html = re.sub(r'src="morgonsol\.js\?v=\d+"', 'src="morgonsol.js?v=%d"' % new, html)
    write("morgonsol.html", html)

    sw = read("sw.js")
    write("sw.js", re.sub(r"const SHELL = 'svampfinder-shell-v\d+';",
                          "const SHELL = 'svampfinder-shell-v%d';" % new, sw, count=1))

    print("build %d -> %d  (morgonsol.js, morgonsol.html, sw.js)" % (old, new))


if __name__ == "__main__":
    main()
