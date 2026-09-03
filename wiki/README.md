# Wiki source

These files are the source of truth for the
[GitHub wiki](https://github.com/shouravx/PeekESP/wiki). They live in the main
repository so wiki changes are reviewable in pull requests and travel with the
code they describe — a browser-edited wiki drifts out of date silently.

## Publishing

GitHub creates the `.wiki.git` repository only after the first page exists, so
this is a one-time manual step:

1. Open <https://github.com/shouravx/PeekESP/wiki> and click
   **Create the first page**. Any content will do — it gets overwritten.
2. Save it.

Then, from the repository root:

```bash
git clone https://github.com/shouravx/PeekESP.wiki.git ../PeekESP.wiki
```

```bash
find wiki -maxdepth 1 -name '*.md' ! -name 'README.md' -exec cp {} ../PeekESP.wiki/ \; && cd ../PeekESP.wiki && git add -A && git commit -m "Sync wiki from main repo" && git push
```

`README.md` is excluded on purpose — it is this note about publishing, not a
wiki page. Copying it would put a stray "README" in the sidebar.

## Naming

Page file names become URLs and link targets, so hyphens matter:
`Configuration-Reference.md` is linked as `[...](Configuration-Reference)`.
`_Sidebar.md` is special — it renders as the navigation on every page.

Edit here, not in the browser. Browser edits will be overwritten by the next
sync.
