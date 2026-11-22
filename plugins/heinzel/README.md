# steuermaschine-heinzel

The Heinzel plugin for [steuermaschine](https://pypi.org/project/steuermaschine/) (`sm`): focused admission, lane caps,
the release recipe and the Mini verification. Install it beside `sm`:

```sh
uv tool install steuermaschine --with steuermaschine-heinzel
```

It registers itself through the `steuermaschine.plugins` entry point. Released together with `steuermaschine`: the same
version, the same workflow, one trusted publisher per PyPI project.
