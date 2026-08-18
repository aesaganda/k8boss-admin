"""
Generic resource access: the §1.2 envelope, API discovery, reads and row shaping.

Four modules, in dependency order:

* :mod:`app.resources.envelope` — the list envelope and :func:`~app.resources.envelope.collect`,
  the mechanism that turns a failed optional read into a recorded ``unavailable``
  entry instead of an empty list. Depends on nothing else here.
* :mod:`app.resources.catalog` — what this cluster actually serves, cached with a
  bounded TTL, and the group/version/plural resolver every read goes through.
* :mod:`app.resources.reader` — list/get/trim/to_yaml over the resolved resource.
* :mod:`app.resources.shaping` — the §8 typed rows plus ``pod_row``/``phase_detail``
  from §6. Pure functions over objects; performs no I/O, so a shaper can never be
  the thing that swallowed an error.

The import direction is strictly downward (``reader`` imports ``catalog`` imports
``envelope``), which is why the single raw REST GET helper lives in ``catalog``
rather than in ``reader``: putting it in ``reader`` would make the two import each
other.

``shaping`` sits *below* all three despite being listed last — it imports nothing
from this package, which is what lets ``reader`` take
``LAST_APPLIED_ANNOTATION`` from it. That constant lives there because
:func:`app.resources.shaping.redact_secret` has to strip the same annotation
``trim`` does: ``kubectl apply`` stores a complete copy of a Secret, values
included, inside it.
"""
