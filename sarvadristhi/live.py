"""Live acquisition from a running recorder over the network (read-only operations only).

ONVIF (Profile S/G): device identity, recording search and replay URIs, with WS-Security
UsernameToken digest. Vendor HTTP APIs download recordings in their native containers,
which the matching plugin then parses with recorder timestamps intact:
  Dahua / CP Plus   GET /cgi-bin/loadfile.cgi?action=startLoad   -> DHAV (.dav)
  Hikvision         POST /ISAPI/ContentMgmt/search + GET /ISAPI/ContentMgmt/download -> PS
FFmpeg cannot send ONVIF's 'Require: onvif-replay' header, so ONVIF replay is offered as a
URI (for a GStreamer/VLC pull) and the vendor APIs are used for byte-exact downloads.
Status: protocol code is tested against a local mock device, not yet against real hardware."""
import base64
import hashlib
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .custody import Ledger

NS = {"s": "http://www.w3.org/2003/05/soap-envelope", "tds": "http://www.onvif.org/ver10/device/wsdl",
      "tse": "http://www.onvif.org/ver10/search/wsdl", "trp": "http://www.onvif.org/ver10/replay/wsdl",
      "tt": "http://www.onvif.org/ver10/schema"}
TIMEOUT = 30


# ------------------------------------------------------------------ ONVIF
def _security(user: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return ('<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
            f"<Username>{user}</Username><Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
            f"oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">{digest}</Password>"
            f"<Nonce EncodingType=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-"
            f"security-1.0#Base64Binary\">{base64.b64encode(nonce).decode()}</Nonce>"
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"{created}</Created></UsernameToken></Security>")


def soap(url: str, body: str, user: str, password: str) -> ET.Element:
    env = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" '
           f'xmlns:tse="{NS["tse"]}" xmlns:trp="{NS["trp"]}" xmlns:tt="{NS["tt"]}">'
           f"<s:Header>{_security(user, password)}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
    req = urllib.request.Request(url, env.encode(), {"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        root = ET.fromstring(r.read())
    fault = root.find(".//s:Fault", NS)
    if fault is not None:
        raise RuntimeError("ONVIF fault: " + " ".join(t.strip() for t in fault.itertext() if t.strip()))
    return root


def _text(el, path):
    x = el.find(path, NS)
    return x.text if x is not None else None


class Onvif:
    def __init__(self, host: str, user: str, password: str, port: int = 80, scheme: str = "http"):
        self.base = f"{scheme}://{host}:{port}"
        self.user, self.password = user, password
        self.xaddr = {"device": f"{self.base}/onvif/device_service"}

    def device_information(self) -> dict:
        r = soap(self.xaddr["device"], "<tds:GetDeviceInformation/>", self.user, self.password)
        i = r.find(".//tds:GetDeviceInformationResponse", NS)
        return {k: _text(i, f"tds:{k}") for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}

    def system_time(self) -> dict:
        r = soap(self.xaddr["device"], "<tds:GetSystemDateAndTime/>", self.user, self.password)
        u = r.find(".//tt:UTCDateTime", NS)
        if u is None:
            return {}
        d = {k: int(_text(u, f"tt:{p}/tt:{k}")) for p, ks in (("Date", ("Year", "Month", "Day")),
                                                                 ("Time", ("Hour", "Minute", "Second"))) for k in ks}
        dev = datetime(d["Year"], d["Month"], d["Day"], d["Hour"], d["Minute"], d["Second"], tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return {"device_utc": dev.isoformat(), "examiner_utc": now.isoformat(timespec="seconds"),
                "device_minus_examiner_s": round((dev - now).total_seconds())}

    def services(self) -> dict:
        r = soap(self.xaddr["device"], "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability>"
                 "</tds:GetServices>", self.user, self.password)
        for s in r.findall(".//tds:Service", NS):
            ns, x = _text(s, "tds:Namespace"), _text(s, "tds:XAddr")
            if ns == NS["tse"]:
                self.xaddr["search"] = x
            elif ns == NS["trp"]:
                self.xaddr["replay"] = x
        return dict(self.xaddr)

    def recordings(self, max_matches: int = 100) -> list[dict]:
        if "search" not in self.xaddr:
            self.services()
        r = soap(self.xaddr["search"], "<tse:FindRecordings><tse:Scope/><tse:MaxMatches>"
                 f"{max_matches}</tse:MaxMatches><tse:KeepAliveTime>PT10S</tse:KeepAliveTime></tse:FindRecordings>",
                 self.user, self.password)
        token = _text(r, ".//tse:SearchToken")
        r = soap(self.xaddr["search"], f"<tse:GetRecordingSearchResults><tse:SearchToken>{token}</tse:SearchToken>"
                 "<tse:WaitTime>PT5S</tse:WaitTime></tse:GetRecordingSearchResults>", self.user, self.password)
        out = []
        for ri in r.findall(".//tt:RecordingInformation", NS):
            out.append({"token": _text(ri, "tt:RecordingToken"), "source": _text(ri, "tt:Source/tt:Name"),
                        "earliest": _text(ri, "tt:EarliestRecording"), "latest": _text(ri, "tt:LatestRecording"),
                        "status": _text(ri, "tt:RecordingStatus")})
        return out

    def replay_uri(self, recording_token: str) -> str:
        if "replay" not in self.xaddr:
            self.services()
        r = soap(self.xaddr["replay"], "<trp:GetReplayUri><trp:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                 "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trp:StreamSetup>"
                 f"<trp:RecordingToken>{recording_token}</trp:RecordingToken></trp:GetReplayUri>",
                 self.user, self.password)
        return _text(r, ".//trp:Uri")


# ------------------------------------------------------------------ vendor HTTP
def _opener(base: str, user: str, password: str):
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pm.add_password(None, base, user, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm),
                                       urllib.request.HTTPBasicAuthHandler(pm))


def _stream_to(resp, out: Path) -> dict:
    md5, sha, n = hashlib.md5(), hashlib.sha256(), 0
    with out.open("xb") as f:
        for b in iter(lambda: resp.read(1 << 20), b""):
            md5.update(b)
            sha.update(b)
            f.write(b)
            n += len(b)
    return {"file": str(out), "bytes": n, "md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def dahua_download(host: str, user: str, password: str, channel: int, start: str, end: str, out: Path,
                   port: int = 80) -> dict:
    """start/end as 'YYYY-MM-DD HH:MM:SS' in recorder time. Returns DHAV bytes."""
    base = f"http://{host}:{port}"
    q = urllib.parse.urlencode({"action": "startLoad", "channel": channel, "startTime": start,
                                "endTime": end, "subtype": 0}, quote_via=urllib.parse.quote)
    url = f"{base}/cgi-bin/loadfile.cgi?{q}"
    with _opener(base, user, password).open(url, timeout=TIMEOUT) as r:
        return {"url": url, **_stream_to(r, out)}


def hikvision_search(host: str, user: str, password: str, track: int, start: str, end: str,
                     port: int = 80) -> list[str]:
    """track e.g. 101 for channel 1 main stream; start/end ISO 'YYYY-MM-DDTHH:MM:SSZ'."""
    base = f"http://{host}:{port}"
    body = (f'<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription><searchID>sarvadristhi-{os.getpid()}</searchID>'
            f"<trackList><trackID>{track}</trackID></trackList><timeSpanList><timeSpan><startTime>{start}</startTime>"
            f"<endTime>{end}</endTime></timeSpan></timeSpanList><maxResults>200</maxResults>"
            "<searchResultPostion>0</searchResultPostion><metadataList><metadataDescriptor>//recordType.meta.std-cgi.com"
            "</metadataDescriptor></metadataList></CMSearchDescription>")
    req = urllib.request.Request(f"{base}/ISAPI/ContentMgmt/search", body.encode(),
                                 {"Content-Type": "application/xml"}, method="POST")
    with _opener(base, user, password).open(req, timeout=TIMEOUT) as r:
        xml = r.read().decode("utf-8", "replace")
    return [urllib.parse.unquote(u).replace("&amp;", "&") for u in re.findall(r"<playbackURI>(.*?)</playbackURI>", xml, re.S)]


def hikvision_download(host: str, user: str, password: str, playback_uri: str, out: Path, port: int = 80) -> dict:
    base = f"http://{host}:{port}"
    body = (f'<?xml version="1.0" encoding="UTF-8"?><downloadRequest><playbackURI>'
            f"{playback_uri.replace('&', '&amp;')}</playbackURI></downloadRequest>")
    req = urllib.request.Request(f"{base}/ISAPI/ContentMgmt/download", body.encode(),
                                 {"Content-Type": "application/xml"}, method="GET")
    with _opener(base, user, password).open(req, timeout=TIMEOUT) as r:
        return {"playback_uri": playback_uri, **_stream_to(r, out)}


# ------------------------------------------------------------------ orchestration
def acquire_live(host: str, user: str, password: str, out_dir: Path, ledger: Ledger, *, vendor: str = "auto",
                 channels=(1,), start: str, end: str, port: int = 80) -> dict:
    """Identify the device over ONVIF, record its clock offset, list recordings, then pull the
    requested channels/time range through the vendor API. Every step goes to the ledger."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"host": host, "downloads": []}
    try:
        ov = Onvif(host, user, password, port)
        result["device"] = ov.device_information()
        result["clock"] = ov.system_time()
        result["services"] = ov.services()
        result["recordings"] = ov.recordings() if "search" in ov.xaddr else []
        ledger.append("live-device-identified", host=host, **result["device"], clock=result["clock"],
                      recordings=len(result["recordings"]))
    except Exception as e:
        result["onvif_error"] = str(e)
        ledger.append("live-onvif-unavailable", host=host, error=str(e)[:300])
    if vendor == "auto":
        mfr = (result.get("device") or {}).get("Manufacturer") or ""
        vendor = "hikvision" if "hik" in mfr.lower() else "dahua" if mfr else "dahua"
    for ch in channels:
        if vendor == "dahua":
            p = out_dir / f"live_ch{ch:02d}_{start.replace(' ', 'T').replace(':', '')}.dav"
            d = dahua_download(host, user, password, ch, start, end, p, port)
        else:
            iso = lambda t: t.replace(" ", "T") + ("Z" if not t.endswith("Z") else "")
            uris = hikvision_search(host, user, password, ch * 100 + 1, iso(start), iso(end), port)
            if not uris:
                ledger.append("live-no-recordings", channel=ch, start=start, end=end)
                continue
            p = out_dir / f"live_ch{ch:02d}_{start.replace(' ', 'T').replace(':', '')}.ps"
            d = hikvision_download(host, user, password, uris[0], p, port)
        d.update(channel=ch, vendor=vendor, start=start, end=end)
        ledger.append("live-download", **d)
        result["downloads"].append(d)
    result["vendor"] = vendor
    return result
