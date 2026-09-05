"""The object filter: which annotations a person is currently being shown.

There is one of these and it is serialisable, because the alternative is the
bug this file exists to prevent. The client hides a class; the `Shift`+`Space`
walk asks the *server* for the next frame that needs a human; the server has
never heard of the class being hidden, and offers a track nobody can see. That
is not a missing check in the walk — it is a decision (what is on screen) made
in a place that cannot tell the other place about it.

So the filter is a small value with the same meaning on both sides:

    {"labels": {"exclude": ["forklift"]},
     "layers": {"exclude": [7]},
     "keys":   {"exclude": [["cam1", "88"]]}}

`display.js` applies it in the browser; this module turns it into SQL. Any
endpoint that *offers an object to a human* takes `?filter=<json>` and honours
it — see `identity.problems` and the proposals queue. Endpoints that merely
return data the client is about to filter itself (a frame window) do not need
it, and should not take it.

Only fields expressible in the core's own vocabulary live here — a label, a
layer, a (stream, key) pair — because those are the only things both sides
already agree on. A plugin's own idea of hidden (a superseded copy of a cut
track) stays in the browser and is enforced by the display authority instead.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ObjectFilter:
    labels_exclude: set = field(default_factory=set)
    labels_include: set | None = None            # None = no opinion
    layers_exclude: set = field(default_factory=set)
    keys_exclude: set = field(default_factory=set)   # {(stream, key)}

    # -- parsing -------------------------------------------------------------
    @classmethod
    def parse(cls, raw: str | dict | None) -> "ObjectFilter":
        """Never raises. A filter that fails to parse means "show everything",
        which is the safe direction: the four doors in the client still hold,
        and a query that silently hid rows would be the worse failure."""
        if not raw:
            return cls()
        try:
            d = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if not isinstance(d, dict):
                return cls()
            lab = d.get("labels") if isinstance(d.get("labels"), dict) else {}
            lay = d.get("layers") if isinstance(d.get("layers"), dict) else {}
            kk = d.get("keys") if isinstance(d.get("keys"), dict) else {}
            keys = kk.get("exclude") or []
            inc = lab.get("include")
            return cls(
                labels_exclude={str(x) for x in (lab.get("exclude") or [])},
                labels_include=None if inc is None else {str(x) for x in inc},
                layers_exclude={int(x) for x in (lay.get("exclude") or []) if _int(x)},
                keys_exclude={(str(a), str(b)) for a, b in keys
                              if isinstance(a, (str, int)) and isinstance(b, (str, int))},
            )
        except (ValueError, TypeError, AttributeError):
            return cls()

    def to_dict(self) -> dict:
        out: dict = {}
        if self.labels_exclude or self.labels_include is not None:
            out["labels"] = {}
            if self.labels_exclude:
                out["labels"]["exclude"] = sorted(self.labels_exclude)
            if self.labels_include is not None:
                out["labels"]["include"] = sorted(self.labels_include)
        if self.layers_exclude:
            out["layers"] = {"exclude": sorted(self.layers_exclude)}
        if self.keys_exclude:
            out["keys"] = {"exclude": [list(k) for k in sorted(self.keys_exclude)]}
        return out

    @property
    def empty(self) -> bool:
        return not (self.labels_exclude or self.layers_exclude or self.keys_exclude
                    or self.labels_include is not None)

    # -- applying ------------------------------------------------------------
    def allows(self, *, label: str = "", layer_id: int | None = None,
               stream: str = "", key: str = "") -> bool:
        if self.labels_include is not None and str(label) not in self.labels_include:
            return False
        if str(label) in self.labels_exclude:
            return False
        if layer_id is not None and int(layer_id) in self.layers_exclude:
            return False
        if (str(stream), str(key)) in self.keys_exclude:
            return False
        return True

    def sql(self, obj: str = "o", layer: str = "l", stream: str = "s") -> tuple[str, list]:
        """A WHERE fragment and its arguments, joinable with AND.

        `obj` must be an alias of `objects`; `stream` is only needed when the
        filter names (stream, key) pairs, and callers that have no streams join
        may pass the pairs by key alone at the cost of being coarser — so the
        fragment simply omits that clause when no alias is given.
        """
        parts, args = [], []
        if self.labels_include is not None:
            marks = ",".join("?" * len(self.labels_include)) or "NULL"
            parts.append(f"{obj}.label IN ({marks})")
            args += sorted(self.labels_include)
        if self.labels_exclude:
            marks = ",".join("?" * len(self.labels_exclude))
            parts.append(f"{obj}.label NOT IN ({marks})")
            args += sorted(self.labels_exclude)
        if self.layers_exclude:
            marks = ",".join("?" * len(self.layers_exclude))
            parts.append(f"{obj}.layer_id NOT IN ({marks})")
            args += sorted(self.layers_exclude)
        if self.keys_exclude and stream:
            for skey, okey in sorted(self.keys_exclude):
                parts.append(f"NOT ({stream}.key = ? AND {obj}.key = ?)")
                args += [skey, okey]
        return (" AND ".join(parts) if parts else "1=1"), args


def _int(x) -> bool:
    try:
        int(x)
        return True
    except (TypeError, ValueError):
        return False
