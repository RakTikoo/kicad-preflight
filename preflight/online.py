"""Opt-in ONLINE part checks (v0.6 WS2) — live distributor data.

This module is NEVER imported-and-run by default: the engine only
calls :func:`online_checks` when the user passes the explicit
``--online`` flag (see v06-ws2/INTEGRATION.md). With the flag off,
no code path in this module runs and no network access happens.

Design rules (fixed by the WS2 spec):
  * Keys come from ENVIRONMENT VARIABLES ONLY
    (``PREFLIGHT_DIGIKEY_CLIENT_ID`` / ``PREFLIGHT_DIGIKEY_CLIENT_SECRET``,
    ``PREFLIGHT_MOUSER_API_KEY``). Never CLI args, files, logs,
    exception messages, finding text, or cache contents. Error
    messages are reason CLASSES only — request URLs are never
    included anywhere (the Mouser key travels in its URL by that
    provider's own design, so a logged URL would be a logged key).
  * Network: stdlib ``urllib`` only, behind a small transport
    callable so tests inject a fake/recorded transport. Explicit
    timeout (10 s, <= 15), response cap (2 MB, abort beyond),
    redirect cap (5), and a User-Agent identifying the tool.
  * Defensive parsing: unknown/missing fields mean "not stated",
    never inferred; wrong types / malformed JSON / oversize payloads
    become a provider failure (a reason class), never a crash.
  * Total provider failure -> zero findings + coverage status
    "unavailable" with the reason class; the offline verdict stands.
  * Same rule ids and thresholds as the offline sample-dataset
    checks (imported from .partrisk) so reports stay comparable.

Providers sit behind one interface: ``lookup(mpn) -> PartInfo``,
raising :class:`PartNotFound` or :class:`ProviderError`.
``RecordedProvider`` replays raw-shaped JSON fixtures through the
same parsers as the live providers, for full offline testing.
"""

import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .finding import BLOCK, CAUTION, NOTE, Finding
from .partrisk import (LONG_LEAD_WEEKS, LOW_STOCK_THRESHOLD,
                       _NRND_STATUSES, _OBSOLETE_STATUSES)

USER_AGENT = "KiCadPreflight/0.6 (+online mode; stdlib urllib)"
TIMEOUT_SECONDS = 10.0            # spec: <= 15 s
MAX_RESPONSE_BYTES = 2_000_000    # spec: <= 2 MB, abort beyond
MAX_REDIRECTS = 5
MAX_ONLINE_MPNS = 200             # per-run lookup budget (audit M5)
ONLINE_DEADLINE_SECONDS = 120.0   # soft wall-clock budget per run
MAX_PRICE_BREAKS = 10_000         # collection caps inside payloads
MAX_PARAMS = 1_000                # (audit L3)
CACHE_MAX_FILE_BYTES = 1_000_000  # a part record is a few KB at most
CACHE_MAX_AGE_SECONDS = 86_400    # 24 h
CACHE_ENV_VAR = "PREFLIGHT_ONLINE_CACHE_DIR"

DIGIKEY_ID_ENV = "PREFLIGHT_DIGIKEY_CLIENT_ID"
DIGIKEY_SECRET_ENV = "PREFLIGHT_DIGIKEY_CLIENT_SECRET"
MOUSER_KEY_ENV = "PREFLIGHT_MOUSER_API_KEY"

DIGIKEY_BASE = "https://api.digikey.com"
DIGIKEY_SANDBOX_BASE = "https://sandbox-api.digikey.com"
MOUSER_BASE = "https://api.mouser.com/api/v1"

REASON_NO_KEY = "no-key"
REASON_NETWORK = "network"
REASON_AUTH = "auth"
REASON_RATE_LIMIT = "rate-limit"
REASON_PROVIDER = "provider-error"

# Lifecycle classification reuses partrisk's exact-status sets
# (whole normalised value, never substring matching — v0.6 review
# RT-08: "Active - not obsolete, no EOL planned" must not fire).


class PartNotFound(Exception):
    """The provider answered, and the MPN is not in its catalogue."""


class ProviderError(Exception):
    """A provider failure, carrying ONLY a reason class — never a
    URL, header, body, or credential fragment."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        super().__init__(reason if not detail else f"{reason}: {detail}")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class _RedirectLimiter(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise ProviderError(REASON_PROVIDER, "too many redirects")
        # Never follow a redirect to a different host (v0.6 audit
        # H1): urllib copies the original request's headers —
        # including Authorization: Bearer and X-DIGIKEY-Client-Id —
        # onto the redirect request verbatim, so a cross-host
        # redirect would hand the provider credential to whoever
        # controls the Location target. Neither provider API needs
        # cross-host redirects; same-host ones still work.
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        new_host = urllib.parse.urlsplit(newurl).hostname
        if (old_host or "").lower() != (new_host or "").lower():
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_transport(method, url, headers, body, timeout):
    """Default transport: (method, url, headers, body, timeout) ->
    (status, headers_dict, body_bytes). Raises ProviderError with a
    reason class on any failure; never includes the URL in messages."""
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    req.add_header("User-Agent", USER_AGENT)
    opener = urllib.request.build_opener(_RedirectLimiter())
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ProviderError(REASON_PROVIDER, "response too large")
            return resp.status, dict(resp.headers.items()), data
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        # HTTPError still carries a readable body; drain capped.
        try:
            data = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            data = b""
        if len(data) > MAX_RESPONSE_BYTES:
            raise ProviderError(REASON_PROVIDER, "response too large")
        return exc.code, dict(exc.headers.items() or {}), data
    except (urllib.error.URLError, TimeoutError, OSError):
        # `from None` (audit L5): the original exception can carry
        # the request URL (for Mouser, the API key) as an attribute;
        # it must not survive as __context__ for any future
        # traceback printing to inherit.
        raise ProviderError(REASON_NETWORK, "request failed") from None


def _call_transport(transport, method, url, headers, body):
    """Run a transport call and map status codes to reason classes.
    Returns parsed JSON (dict) on a 2xx. The URL is never surfaced."""
    try:
        status, _resp_headers, data = transport(
            method, url, headers or {}, body, TIMEOUT_SECONDS)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError(REASON_NETWORK, "transport failed") from None
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if len(data or b"") > MAX_RESPONSE_BYTES:
        raise ProviderError(REASON_PROVIDER, "response too large")
    if status == 429:
        raise ProviderError(REASON_RATE_LIMIT, "http 429")
    if status in (401, 403):
        raise ProviderError(REASON_AUTH, f"http {status}")
    if status == 404:
        raise PartNotFound()
    if not (200 <= int(status) < 300):
        raise ProviderError(REASON_PROVIDER, f"http {status}")
    try:
        parsed = json.loads((data or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProviderError(REASON_PROVIDER, "malformed JSON response")
    if not isinstance(parsed, dict):
        raise ProviderError(REASON_PROVIDER, "unexpected response shape")
    return parsed


# ---------------------------------------------------------------------------
# Normalised part record
# ---------------------------------------------------------------------------

@dataclass
class PartInfo:
    mpn: str
    provider: str
    stock: object = None            # int or None (not stated)
    status: str = None              # raw lifecycle string or None
    lead_weeks: float = None
    lead_raw: str = None
    price_breaks: list = field(default_factory=list)  # (qty, price, currency)
    currency: str = None
    params: dict = field(default_factory=dict)  # normalised name -> raw value
    package: str = None
    description: str = None
    recorded: bool = False          # replayed fixture, not a live fetch

    def to_cache_dict(self):
        return {"mpn": self.mpn, "provider": self.provider,
                "stock": self.stock, "status": self.status,
                "lead_weeks": self.lead_weeks, "lead_raw": self.lead_raw,
                "price_breaks": [list(b) for b in self.price_breaks],
                "currency": self.currency, "params": self.params,
                "package": self.package, "description": self.description}

    @classmethod
    def from_cache_dict(cls, data, provider):
        """Rebuild a PartInfo from a cache record, validating EVERY
        field's type (v0.6 review RT-09 / audit M4): a cache file is
        user-writable state, and a wrong-typed field must degrade to
        a cache miss, never crash the run. Returns None on any
        structural problem."""
        if not isinstance(data, dict) or data.get("provider") != provider:
            return None
        try:
            breaks = [(int(q), float(p), c) for q, p, c in
                      (data.get("price_breaks") or [])[:MAX_PRICE_BREAKS]]
        except (TypeError, ValueError):
            return None
        if any(not isinstance(c, str) or p <= 0 for _q, p, c in breaks):
            return None
        stock = data.get("stock")
        if stock is not None and (isinstance(stock, bool)
                                  or not isinstance(stock, int)
                                  or stock < 0):
            return None
        lead = data.get("lead_weeks")
        if lead is not None and (isinstance(lead, bool)
                                 or not isinstance(lead, (int, float))
                                 or lead < 0):
            return None
        status = data.get("status")
        if status is not None and not isinstance(status, str):
            return None
        params = data.get("params")
        if not isinstance(params, dict):
            return None
        params = {str(k): v for k, v in params.items()
                  if isinstance(v, str)}
        if len(params) > MAX_PARAMS:
            return None

        def _opt_str(key):
            value = data.get(key)
            return value if isinstance(value, str) else None

        return cls(mpn=str(data.get("mpn") or ""), provider=provider,
                   stock=stock, status=status,
                   lead_weeks=float(lead) if lead is not None else None,
                   lead_raw=_opt_str("lead_raw"), price_breaks=breaks,
                   currency=_opt_str("currency"), params=params,
                   package=_opt_str("package"),
                   description=_opt_str("description"))


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        # A plain float string first ("1e5" is 100000, not 1 —
        # v0.6 review RT-13), then a digit run ("10,000 pcs").
        try:
            num = float(value.strip().replace(",", ""))
            return int(num) if num.is_integer() else None
        except ValueError:
            pass
        m = re.search(r"-?\d[\d,]*", value)
        if m:
            try:
                return int(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _valid_stock(value):
    """A parsed stock count is usable only when non-negative; a
    negative count is corrupt data, i.e. 'not stated' (RT-01)."""
    return value if (isinstance(value, int) and value >= 0) else None


def _as_price(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = re.sub(r"[^\d.,-]", "", value).strip()
        if not text:
            return None
        # "0,371" (decimal comma, no dot) -> 0.371; "1,234.56" -> 1234.56
        if "," in text and "." not in text:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _lead_weeks_from_text(text):
    """'6 weeks' -> 6.0 ; '56 Days' -> 8.0 ; '6' -> 6.0 (weeks, the
    DigiKey ManufacturerLeadWeeks convention). Unparseable -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text >= 0 else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(week|wk|day|d)\w*", str(text),
                  re.IGNORECASE)
    if m:
        num = float(m.group(1))
        return num / 7.0 if m.group(2).lower().startswith("d") else num
    m = re.search(r"^\s*(\d+(?:\.\d+)?)\s*$", str(text))
    return float(m.group(1)) if m else None


def _norm_param_name(name):
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


_PACKAGE_PARAM_NAMES = {"package case", "case code", "package",
                        "supplier device package"}


def _params_from_pairs(pairs):
    """pairs: iterable of (name, value). Returns (params, package)."""
    params, package = {}, None
    for name, value in list(pairs)[:MAX_PARAMS]:
        if value is None:
            continue
        key = _norm_param_name(name)
        if not key:
            continue
        params[key] = str(value)
        if key in _PACKAGE_PARAM_NAMES and package is None:
            package = str(value)
    return params, package


# ---------------------------------------------------------------------------
# Provider response parsers (shared by live + recorded)
# ---------------------------------------------------------------------------

def parse_digikey_response(data, mpn, currency="USD"):
    """DigiKey Product Information v4 shapes: a details response
    {"Product": {...}}, a keyword response {"Products": [...]}, or a
    bare product object. Raises PartNotFound / ProviderError."""
    if not isinstance(data, dict):
        raise ProviderError(REASON_PROVIDER, "unexpected response shape")
    product = data.get("Product")
    if product is None and isinstance(data.get("Products"), list):
        wanted = mpn.lower()
        for cand in data["Products"]:
            if isinstance(cand, dict) and str(
                    cand.get("ManufacturerProductNumber", "")).lower() == wanted:
                product = cand
                break
        # No exact-MPN match among search results means the part is
        # not carried under this MPN (RT-07): never accept an
        # unrelated first result as the requested part.
    if product is None and ("QuantityAvailable" in data
                            or "ManufacturerProductNumber" in data):
        product = data
    if not isinstance(product, dict):
        raise PartNotFound()
    stated_mpn = product.get("ManufacturerProductNumber")
    if isinstance(stated_mpn, str) and stated_mpn \
            and stated_mpn.lower() != mpn.lower():
        raise PartNotFound()
    info = PartInfo(mpn=mpn, provider="digikey")
    info.stock = _valid_stock(_as_int(product.get("QuantityAvailable")))
    status = product.get("ProductStatus")
    if isinstance(status, dict):
        info.status = str(status.get("Status") or "") or None
    elif isinstance(status, str):
        info.status = status or None
    if product.get("Discontinued") is True and not info.status:
        info.status = "Discontinued"
    if product.get("EndOfLife") is True and not info.status:
        info.status = "End of Life"
    lead_raw = product.get("ManufacturerLeadWeeks")
    info.lead_weeks = _lead_weeks_from_text(lead_raw)
    info.lead_raw = str(lead_raw) if lead_raw is not None else None
    # Pricing lives under product variations in v4. Non-positive
    # prices/quantities are corrupt data, not prices (RT-13).
    breaks, stated_currency = [], None
    variations = product.get("ProductVariations")
    if isinstance(variations, list):
        for var in variations:
            if not isinstance(var, dict):
                continue
            for br in (var.get("StandardPricing") or [])[:MAX_PRICE_BREAKS]:
                if isinstance(br, dict):
                    qty, price = _as_int(br.get("BreakQuantity")), \
                        _as_price(br.get("UnitPrice"))
                    if qty is not None and qty > 0 \
                            and price is not None and price > 0:
                        breaks.append((qty, price, currency))
    for br in (product.get("StandardPricing") or [])[:MAX_PRICE_BREAKS]:
        if isinstance(br, dict):
            qty, price = _as_int(br.get("BreakQuantity")), \
                _as_price(br.get("UnitPrice"))
            if qty is not None and qty > 0 \
                    and price is not None and price > 0:
                breaks.append((qty, price, currency))
    info.price_breaks = sorted(set(breaks))[:MAX_PRICE_BREAKS]
    info.currency = stated_currency or (currency if breaks else None)
    desc = product.get("Description")
    if isinstance(desc, dict):
        info.description = desc.get("ProductDescription") or None
    elif isinstance(desc, str):
        info.description = desc
    pairs = []
    for param in product.get("Parameters") or []:
        if isinstance(param, dict):
            pairs.append((param.get("ParameterText"),
                          param.get("ValueText")))
    info.params, info.package = _params_from_pairs(pairs)
    return info


def parse_mouser_response(data, mpn):
    """Mouser Search API shape: {"SearchResults": {"Parts": [...]},
    "Errors": [...]}. Raises PartNotFound / ProviderError."""
    if not isinstance(data, dict):
        raise ProviderError(REASON_PROVIDER, "unexpected response shape")
    errors = data.get("Errors")
    if isinstance(errors, list) and any(isinstance(e, dict) for e in errors):
        raise ProviderError(REASON_PROVIDER, "provider returned errors")
    results = data.get("SearchResults")
    parts = results.get("Parts") if isinstance(results, dict) else None
    if not isinstance(parts, list) or not parts:
        raise PartNotFound()
    wanted = mpn.lower()
    part = None
    for cand in parts:
        if isinstance(cand, dict) and str(
                cand.get("ManufacturerPartNumber", "")).lower() == wanted:
            part = cand
            break
    if part is None:
        # No exact-MPN match: not carried under this MPN (RT-07).
        raise PartNotFound()
    info = PartInfo(mpn=mpn, provider="mouser")
    info.stock = _valid_stock(_as_int(part.get("AvailabilityInStock")
                              if part.get("AvailabilityInStock") is not None
                              else part.get("Availability")))
    info.status = str(part.get("LifecycleStatus") or "") or None
    info.lead_weeks = _lead_weeks_from_text(part.get("LeadTime"))
    info.lead_raw = str(part["LeadTime"]) if part.get("LeadTime") else None
    breaks = []
    for br in (part.get("PriceBreaks") or [])[:MAX_PRICE_BREAKS]:
        if isinstance(br, dict):
            qty, price = _as_int(br.get("Quantity")), _as_price(br.get("Price"))
            if qty is not None and qty > 0 \
                    and price is not None and price > 0:
                breaks.append((qty, price, str(br.get("Currency") or "USD")))
    info.price_breaks = sorted(set(breaks))[:MAX_PRICE_BREAKS]
    info.currency = breaks[0][2] if breaks else None
    info.description = str(part.get("Description") or "") or None
    pairs = []
    for attr in part.get("ProductAttributes") or []:
        if isinstance(attr, dict):
            pairs.append((attr.get("AttributeName"),
                          attr.get("AttributeValue")))
    info.params, info.package = _params_from_pairs(pairs)
    return info


_PARSERS = {"digikey": parse_digikey_response, "mouser": parse_mouser_response}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class DigikeyProvider:
    name = "digikey"

    def __init__(self, env, transport, base=DIGIKEY_BASE):
        self._client_id = (env or {}).get(DIGIKEY_ID_ENV) or ""
        self._client_secret = (env or {}).get(DIGIKEY_SECRET_ENV) or ""
        if not self._client_id or not self._client_secret:
            raise ProviderError(REASON_NO_KEY,
                                "DigiKey credentials not configured")
        self._transport = transport
        self._base = base
        self._token = None            # memory only, never persisted

    def _get_token(self):
        if self._token:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials"}).encode("utf-8")
        data = _call_transport(
            self._transport, "POST", f"{self._base}/v1/oauth2/token",
            {"Content-Type": "application/x-www-form-urlencoded"}, body)
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ProviderError(REASON_PROVIDER, "no access token returned")
        self._token = token
        return token

    def lookup(self, mpn):
        token = self._get_token()
        url = (f"{self._base}/products/v4/search/"
               f"{urllib.parse.quote(mpn, safe='')}/productdetails")
        headers = {"Authorization": f"Bearer {token}",
                   "X-DIGIKEY-Client-Id": self._client_id,
                   "X-DIGIKEY-Locale-Site": "US",
                   "X-DIGIKEY-Locale-Language": "en",
                   "X-DIGIKEY-Locale-Currency": "USD",
                   "Accept": "application/json"}
        data = _call_transport(self._transport, "GET", url, headers, None)
        return parse_digikey_response(data, mpn)


class MouserProvider:
    name = "mouser"

    def __init__(self, env, transport, base=MOUSER_BASE):
        self._api_key = (env or {}).get(MOUSER_KEY_ENV) or ""
        if not self._api_key:
            raise ProviderError(REASON_NO_KEY,
                                "Mouser API key not configured")
        self._transport = transport
        self._base = base

    def lookup(self, mpn):
        # The key travels in the query string by Mouser's design; the
        # URL is therefore never logged, returned, or put in an error.
        url = (f"{self._base}/search/partnumber?apiKey="
               f"{urllib.parse.quote(self._api_key, safe='')}")
        body = json.dumps({"SearchByPartRequest": {
            "mouserPartNumber": mpn,
            "partSearchOptions": "Exact"}}).encode("utf-8")
        data = _call_transport(self._transport, "POST", url,
                               {"Content-Type": "application/json",
                                "Accept": "application/json"}, body)
        return parse_mouser_response(data, mpn)


class RecordedProvider:
    """Replays raw-shaped provider fixtures through the live parsers.
    ``fixtures``: {MPN: raw response dict} for one provider."""

    def __init__(self, provider_name, fixtures):
        if provider_name not in _PARSERS:
            raise ValueError(f"unknown recorded provider {provider_name!r}")
        self.name = provider_name
        self._fixtures = {str(k).lower(): v for k, v in fixtures.items()}
        self.calls = 0

    @classmethod
    def from_directory(cls, provider_name, directory):
        path = Path(directory) / f"{provider_name}_responses.json"
        try:
            if path.stat().st_size > MAX_RESPONSE_BYTES:
                raise ProviderError(REASON_PROVIDER,
                                    "recorded fixture too large")
            data = json.loads(path.read_text(encoding="utf-8"))
        except ProviderError:
            raise
        except (OSError, ValueError, RecursionError):
            # A corrupt fixture degrades like any provider failure
            # (RT-14): the caller reports "online unavailable" and
            # the offline verdict stands — never a raw traceback.
            raise ProviderError(REASON_PROVIDER,
                                "recorded fixture unreadable") from None
        if not isinstance(data, dict):
            raise ProviderError(REASON_PROVIDER,
                                "recorded fixture has the wrong shape")
        return cls(provider_name, data)

    def lookup(self, mpn):
        self.calls += 1
        raw = self._fixtures.get(mpn.lower())
        if raw is None:
            raise PartNotFound()
        if not isinstance(raw, dict):
            raise ProviderError(REASON_PROVIDER, "bad recorded fixture")
        info = _PARSERS[self.name](raw, mpn)
        info.recorded = True
        return info


def _make_live_provider(provider_name, env, transport):
    if provider_name == "digikey":
        return DigikeyProvider(env, transport)
    if provider_name == "mouser":
        return MouserProvider(env, transport)
    raise ValueError(f"unknown provider {provider_name!r}")


def _select_live_provider(env, transport, provider_name):
    """Returns a provider or raises ProviderError(no-key)."""
    if provider_name in ("digikey", "mouser"):
        return _make_live_provider(provider_name, env, transport)
    # auto: DigiKey first (richer parametrics), then Mouser.
    for name in ("digikey", "mouser"):
        try:
            return _make_live_provider(name, env, transport)
        except ProviderError as exc:
            if exc.reason != REASON_NO_KEY:
                raise
    raise ProviderError(REASON_NO_KEY, "no provider credentials configured")


# ---------------------------------------------------------------------------
# Cache (parsed fields only — see provider-research.md)
# ---------------------------------------------------------------------------

def _cache_path(cache_dir, provider, mpn):
    key = hashlib.sha256(f"{provider}\0{mpn.upper()}".encode()).hexdigest()
    return Path(cache_dir) / f"{key}.json"


def _cache_read(cache_dir, provider, mpn, now_ts):
    if not cache_dir:
        return None, None
    try:
        path = _cache_path(cache_dir, provider, mpn)
        if path.stat().st_size > CACHE_MAX_FILE_BYTES:
            return None, None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    fetched = data.get("fetched_at")
    if not isinstance(fetched, (int, float)) or isinstance(fetched, bool):
        return None, None
    if now_ts - fetched > CACHE_MAX_AGE_SECONDS or fetched > now_ts:
        return None, None
    info = PartInfo.from_cache_dict(data.get("part"), provider)
    if info is None:
        return None, None
    return info, now_ts - fetched


def _cache_write(cache_dir, info, now_ts):
    if not cache_dir:
        return
    try:
        # Hardened against a shared/predictable cache location
        # (audit M3): the directory is created private (0700) and
        # files are written with O_NOFOLLOW at 0600, so a planted
        # symlink at the (hash-predictable) cache path cannot
        # redirect the write into an unrelated file. Any failure
        # just skips caching — a broken cache must never break the
        # check.
        Path(cache_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"fetched_at": now_ts, "part": info.to_cache_dict()}
        text = json.dumps(payload, indent=1)
        path = _cache_path(cache_dir, info.provider, info.mpn)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass        # a broken cache must never break the check


def _resolve_cache_dir(cache_dir, env):
    if cache_dir is not None:
        return Path(cache_dir)
    from_env = (env or {}).get(CACHE_ENV_VAR)
    if from_env:
        return Path(from_env)
    return Path(tempfile.gettempdir()) / "kicad-preflight-online-cache"


# ---------------------------------------------------------------------------
# Passive value parsing (semantic check)
# ---------------------------------------------------------------------------

_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
         "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def _parse_scaled(text, decimal_letter):
    """Parse an engineering value: '10k' -> (10000, rest), '4R7' with
    decimal_letter='R' -> 4.7, '2.2k' -> 2200. Returns float or None."""
    if text is None:
        return None
    s = str(text).strip().replace(" ", "").replace(",", "")
    s = s.replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    if decimal_letter:
        m = re.match(r"^(\d+)" + re.escape(decimal_letter) + r"(\d+)$", s)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
    m = re.match(r"^(\d+(?:\.\d+)?)([pnuµμmkKMG])?$", s)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2):
        value *= _MULT[m.group(2)]
    return value


def parse_resistance(text):
    """'10k', '10 kΩ', '4R7', '100R', '330' -> ohms (float) or None."""
    if text is None:
        return None
    s = str(text).strip()
    embedded = re.match(r"^(\d+)R(\d+)$", s.replace(" ", ""))
    if embedded:
        return float(f"{embedded.group(1)}.{embedded.group(2)}")
    s = re.sub(r"(?i)ohms?|Ω", "", s).strip()
    if s.upper().endswith("R") and len(s) > 1:
        s = s[:-1]
    return _parse_scaled(s, None)


def parse_capacitance(text):
    """'100n', '10uF', '4p7', '100nF' -> farads or None. A bare number
    is ambiguous for capacitors and is NOT guessed."""
    if text is None:
        return None
    s = str(text).strip().replace(" ", "")
    s = re.sub(r"(?i)farads?$|F$", "", s.replace("µ", "u").replace("μ", "u"))
    if not re.search(r"[pnuµμmkKM]", s):
        return None
    embedded = re.match(r"^(\d+)([pnu])(\d+)$", s)
    if embedded:
        return float(f"{embedded.group(1)}.{embedded.group(3)}") \
            * _MULT[embedded.group(2)]
    return _parse_scaled(s.replace("µ", "u").replace("μ", "u"), None)


def parse_inductance(text):
    """'10uH', '4.7uH', '100nH', '1mH' -> henries or None."""
    if text is None:
        return None
    s = str(text).strip().replace(" ", "")
    s = re.sub(r"(?i)henr\w*$|H$", "", s.replace("µ", "u").replace("μ", "u"))
    if not re.search(r"[pnuµμmkKM]", s):
        return None
    return _parse_scaled(s, None)


_VALUE_PARSERS = {"resistance": parse_resistance,
                  "capacitance": parse_capacitance,
                  "inductance": parse_inductance}
_KIND_BY_REF = {"R": "resistance", "C": "capacitance", "L": "inductance"}
_PARAM_BY_KIND = {"resistance": "resistance", "capacitance": "capacitance",
                  "inductance": "inductance"}
_CHIP_SIZES = {"0201", "0402", "0603", "0805", "1206", "1210", "1812",
               "2010", "2512", "0612", "1020"}


def _package_code(text):
    if not text:
        return None
    for token in re.findall(r"\d{4}", str(text)):
        if token in _CHIP_SIZES:
            return token
    return None


def _bom_kind(row):
    for ref in (getattr(row, "refs", None) or []):
        letter = re.match(r"([A-Za-z]+)", str(ref))
        if letter and letter.group(1)[:1].upper() in _KIND_BY_REF:
            return _KIND_BY_REF[letter.group(1)[:1].upper()]
    value = (getattr(row, "value", "") or "").lower()
    if "f" in value and re.search(r"[pnuµμ]\s*f?\b", value):
        return "capacitance"
    return None


def _close(a, b):
    return a is not None and b is not None and math.isclose(
        a, b, rel_tol=1e-6, abs_tol=1e-15)


def _fmt_si(value, unit):
    if value is None:
        return "?"
    for prefix, mult in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1.0),
                         ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(value) >= mult and (mult == 1e-12 or abs(value) < mult * 1000):
            num = value / mult
            return f"{num:g}{prefix}{unit}"
    return f"{value:g}{unit}"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _now_ts(now):
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    return float(now.timestamp())


def _age_text(age_seconds, recorded):
    if recorded:
        return "recorded fixture (not live)"
    if age_seconds is None or age_seconds < 5:
        return "live, fetched just now"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m old (cached)"
    return f"{age_seconds / 3600:.1f}h old (cached)"


def online_checks(bom_rows, project_qty_context=None, transport=None,
                  env=None, cache_dir=None, now=None, provider=None,
                  recorded_fixture=None):
    """Run the online checks over parsed BOM rows.

    bom_rows: BomRow-like objects (.mpn, .qty, .refs, .value,
    .footprint, .line_no).
    project_qty_context: order-quantity override — an int (same
    quantity for every MPN) or a dict {MPN: qty}. Default (None):
    the BOM quantity itself, summed across rows sharing an MPN —
    i.e. "how many of this part one board order needs", which is
    the quantity the price break is quoted at. Documented choice:
    Preflight does not know how many boards will be built, so it
    quotes at the per-order BOM quantity rather than guessing a
    production volume.
    transport: callable with urllib_transport's signature; tests
    inject a fake/recorded one. None -> the real urllib transport.
    provider: None (auto: DigiKey then Mouser, by configured keys),
    "digikey", "mouser", or a provider object (lookup interface) —
    the last is how RecordedProvider / test doubles plug in.
    recorded_fixture: path to a samples/online-style directory; with
    provider="digikey"/"mouser" builds a RecordedProvider from it.

    Returns (findings, coverage_dict). On TOTAL failure findings is
    [] and coverage["online"]["status"] == "unavailable" with a
    "reason" class — the caller's offline findings stand untouched.
    """
    env = os.environ if env is None else env
    now_ts = _now_ts(now)
    coverage = {"online": {"status": "unavailable", "reason": None,
                           "provider": None, "mpns": 0, "answered": 0,
                           "not_found": 0, "failed": 0, "from_cache": 0,
                           "succeeded_mpns": [], "known_mpns": [],
                           "found_without_risk_data": 0,
                           "mpns_over_budget": 0,
                           "semantic": {"passive_rows": 0,
                                        "non_passive_skipped": 0,
                                        "compared_attributes": 0,
                                        "unstated_attributes": 0,
                                        "mismatches": 0}}}
    online_cov = coverage["online"]
    sem = online_cov["semantic"]

    by_mpn = {}
    for row in bom_rows or []:
        mpn = (getattr(row, "mpn", "") or "").strip()
        if mpn:
            by_mpn.setdefault(mpn, []).append(row)
    online_cov["mpns"] = len(by_mpn)
    if not by_mpn:
        online_cov["status"] = "no-mpns"
        return [], coverage

    # --- resolve provider -------------------------------------------------
    cache_root = _resolve_cache_dir(cache_dir, env)
    prov = None
    if provider is not None and hasattr(provider, "lookup"):
        prov = provider
    elif recorded_fixture is not None:
        name = provider if provider in _PARSERS else "digikey"
        try:
            prov = RecordedProvider.from_directory(name, recorded_fixture)
        except ProviderError as exc:
            # A corrupt fixture degrades exactly like a total
            # provider failure (RT-14): unavailable, offline stands.
            online_cov["reason"] = exc.reason
            return [], coverage
    else:
        transport = transport or urllib_transport
        try:
            prov = _select_live_provider(env, transport, provider)
        except ProviderError as exc:
            online_cov["reason"] = exc.reason
            return [], coverage
    online_cov["provider"] = prov.name
    # The disk cache exists to spare live-provider rate limits.
    # Recorded fixtures and injected test doubles are never cached:
    # replaying them is free, and caching them would leak state
    # between runs.
    use_cache = isinstance(prov, (DigikeyProvider, MouserProvider))

    findings = []
    first_failure_reason = None
    # Per-run budgets (audit M5): a BOM can name unbounded distinct
    # MPNs and a slow endpoint can stretch every socket timeout;
    # cap both the lookup count and the wall clock. MPNs past the
    # budget are counted as failed with the budget reason — the
    # offline result for them stands, and Coverage says why.
    deadline = time.monotonic() + ONLINE_DEADLINE_SECONDS
    ordered_mpns = sorted(by_mpn)
    if len(ordered_mpns) > MAX_ONLINE_MPNS:
        online_cov["mpns_over_budget"] = len(ordered_mpns) - MAX_ONLINE_MPNS
        online_cov["failed"] += online_cov["mpns_over_budget"]
        first_failure_reason = "budget"
        ordered_mpns = ordered_mpns[:MAX_ONLINE_MPNS]
    for mpn in ordered_mpns:
        if time.monotonic() > deadline:
            remaining = len(ordered_mpns) - ordered_mpns.index(mpn)
            online_cov["failed"] += remaining
            if first_failure_reason is None:
                first_failure_reason = "deadline"
            break
        rows = by_mpn[mpn]
        first = rows[0]
        refs = [r for row in rows for r in (getattr(row, "refs", None) or [])]
        where = f"BOM line {getattr(first, 'line_no', '?')} · {mpn}"
        usage = f"{mpn} ({', '.join(refs) or '?'})"

        info, age = None, None
        if use_cache:
            info, age = _cache_read(cache_root, prov.name, mpn, now_ts)
            if info is not None:
                online_cov["from_cache"] += 1
        if info is None:
            try:
                info = prov.lookup(mpn)
                age = 0.0
                if use_cache:
                    _cache_write(cache_root, info, now_ts)
            except PartNotFound:
                online_cov["not_found"] += 1
                online_cov["answered"] += 1
                findings.append(Finding(
                    "ONLINE_PART_NOT_FOUND", NOTE,
                    f"{usage} was not found at {prov.name}, so its "
                    "live lifecycle/stock could not be checked. The "
                    "offline sample-dataset result for this part "
                    "stands unchanged — a catalogue miss says nothing "
                    "by itself (check the MPN spelling).",
                    where=where, evidence=f"provider={prov.name}"))
                continue
            except ProviderError as exc:
                online_cov["failed"] += 1
                if first_failure_reason is None:
                    first_failure_reason = exc.reason
                continue
        online_cov["answered"] += 1
        online_cov["known_mpns"].append(mpn)
        # Supersede eligibility (RT-01): only an answer that states
        # at least one usable risk field (lifecycle status, stock,
        # lead time) may replace the offline part-risk family. An
        # answer that states none of them is recorded as 'known'
        # (it still supersedes PART_NOT_IN_SAMPLE_DATA in the
        # engine merge) but clears no risk finding.
        if (info.status or "").strip() or info.stock is not None \
                or info.lead_weeks is not None:
            online_cov["succeeded_mpns"].append(mpn)
        else:
            online_cov["found_without_risk_data"] += 1
        age_txt = _age_text(age, getattr(info, "recorded", False))
        src = f"provider={prov.name}; data_age={age_txt}"

        # --- lifecycle / stock / lead (same ids+thresholds as partrisk) --
        # Whole-value set membership on the normalised status, the
        # same classification partrisk applies to dataset statuses
        # (RT-08: substring matching false-fires on negated text
        # like "not obsolete").
        status_norm = " ".join((info.status or "").lower().split())
        obsolete = status_norm in _OBSOLETE_STATUSES
        if obsolete:
            findings.append(Finding(
                "PART_OBSOLETE", BLOCK,
                f"{usage} is marked '{info.status}' by {prov.name} "
                f"({age_txt}). An obsolete part can vanish from stock "
                "before your next order — pick a current replacement "
                "now, while changing it is cheap.",
                where=where, evidence=f"status={info.status}; {src}"))
        elif status_norm in _NRND_STATUSES:
            findings.append(Finding(
                "PART_NOT_RECOMMENDED", CAUTION,
                f"{usage} is 'not recommended for new designs' "
                f"according to {prov.name} ({age_txt}). Fine for a "
                "one-off build, a poor choice if you plan to order "
                "this board again.",
                where=where, evidence=f"status={info.status}; {src}"))
        if not obsolete:
            if info.stock == 0:
                findings.append(Finding(
                    "PART_OUT_OF_STOCK", BLOCK,
                    f"{usage} shows 0 in stock at {prov.name} "
                    f"({age_txt}). You cannot order this part today.",
                    where=where, evidence=f"stock=0; {src}"))
            elif info.stock is not None and 0 < info.stock < LOW_STOCK_THRESHOLD:
                findings.append(Finding(
                    "PART_LOW_STOCK", CAUTION,
                    f"{usage} shows only {info.stock} in stock at "
                    f"{prov.name} ({age_txt}) — fewer than the "
                    f"{LOW_STOCK_THRESHOLD}-piece comfort line.",
                    where=where, evidence=f"stock={info.stock}; {src}"))
            if info.lead_weeks is not None and info.lead_weeks > LONG_LEAD_WEEKS:
                findings.append(Finding(
                    "PART_LONG_LEAD_TIME", CAUTION,
                    f"{usage} has a {info.lead_weeks:g}-week lead time "
                    f"at {prov.name} ({age_txt}). Order early or pick "
                    "a faster part if this board is on a schedule.",
                    where=where,
                    evidence=f"lead_time_weeks={info.lead_weeks:g}; {src}"))

        # --- price at the order quantity ---------------------------------
        if isinstance(project_qty_context, dict):
            order_qty = project_qty_context.get(mpn)
        elif isinstance(project_qty_context, int):
            order_qty = project_qty_context
        else:
            order_qty = None
        if order_qty is None:
            order_qty = sum((getattr(r, "qty", None) or len(
                getattr(r, "refs", None) or []) or 1) for r in rows)
        if info.price_breaks:
            eligible = [b for b in info.price_breaks if b[0] <= order_qty]
            qty_b, price, curr = (eligible[-1] if eligible
                                  else info.price_breaks[0])
            findings.append(Finding(
                "ONLINE_PRICE", NOTE,
                f"{usage}: {prov.name} unit price {price:g} {curr} at "
                f"the {qty_b}-piece break (your BOM quantity is "
                f"{order_qty}); data {age_txt}. Prices change — this "
                "is a snapshot, not a quote.",
                where=where,
                evidence=f"unit_price={price:g}; currency={curr}; "
                         f"break_qty={qty_b}; order_qty={order_qty}; {src}"))

        # --- semantic value <-> MPN for passives --------------------------
        kind = _bom_kind(first)
        if kind is None:
            sem["non_passive_skipped"] += 1
        else:
            sem["passive_rows"] += 1
            parser = _VALUE_PARSERS[kind]
            unit = {"resistance": "Ω", "capacitance": "F",
                    "inductance": "H"}[kind]
            bom_val = parser(getattr(first, "value", ""))
            cat_raw = info.params.get(_PARAM_BY_KIND[kind])
            cat_val = parser(cat_raw) if cat_raw else None
            mismatches = []
            if bom_val is not None and cat_val is not None:
                sem["compared_attributes"] += 1
                if not _close(bom_val, cat_val):
                    mismatches.append(
                        f"{kind}: BOM value '{first.value}' "
                        f"({_fmt_si(bom_val, unit)}) vs {prov.name} "
                        f"value '{cat_raw}' ({_fmt_si(cat_val, unit)})")
            else:
                sem["unstated_attributes"] += 1
            # tolerance — only when BOTH sides state it
            bom_tol = re.search(r"(\d+(?:\.\d+)?)\s*%",
                                getattr(first, "value", "") or "")
            cat_tol_raw = info.params.get("tolerance")
            cat_tol = re.search(r"(\d+(?:\.\d+)?)\s*%", cat_tol_raw or "")
            if bom_tol and cat_tol:
                sem["compared_attributes"] += 1
                if not _close(float(bom_tol.group(1)),
                              float(cat_tol.group(1))):
                    mismatches.append(
                        f"tolerance: BOM {bom_tol.group(1)}% vs "
                        f"{prov.name} {cat_tol.group(1)}%")
            elif bom_tol or cat_tol:
                sem["unstated_attributes"] += 1
            # package — only when BOTH sides state a chip size
            bom_pkg = _package_code(getattr(first, "footprint", ""))
            cat_pkg = _package_code(info.package)
            if bom_pkg and cat_pkg:
                sem["compared_attributes"] += 1
                if bom_pkg != cat_pkg:
                    mismatches.append(
                        f"package: BOM footprint is {bom_pkg} vs "
                        f"{prov.name} package '{info.package}' ({cat_pkg})")
            elif bom_pkg or cat_pkg:
                sem["unstated_attributes"] += 1
            if mismatches:
                sem["mismatches"] += 1
                findings.append(Finding(
                    "SEMANTIC_VALUE_MISMATCH", CAUTION,
                    f"{usage}: the BOM describes a different part "
                    f"than {prov.name} says this MPN is — "
                    + "; ".join(mismatches)
                    + ". One of them is wrong and the order would "
                      "bring the catalogue part, not the BOM part.",
                    where=where, evidence=f"{'; '.join(mismatches)}; {src}"))

    if online_cov["answered"] == 0 and online_cov["failed"] > 0:
        # TOTAL failure: no findings at all (incl. no NOT_FOUND notes,
        # of which there are none by construction) and unavailable.
        online_cov["status"] = "unavailable"
        online_cov["reason"] = first_failure_reason or REASON_PROVIDER
        return [], coverage
    online_cov["status"] = "partial" if online_cov["failed"] else "ok"
    if online_cov["failed"]:
        online_cov["reason"] = first_failure_reason
    return findings, coverage
