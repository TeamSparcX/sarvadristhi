"""A mock network video recorder for protocol tests. It checks ONVIF WS-Security digests and
HTTP Digest auth, and serves evidence_builder recordings through Dahua and Hikvision download APIs."""
import base64
import hashlib
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sarvadristhi.formats import dhav  # noqa: E402

REALM = "mock-nvr"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class MockNVR:
    def __init__(self, exports: Path, user="admin", password="secret1234", manufacturer="Dahua"):
        self.exports, self.user, self.password, self.mfr = Path(exports), user, password, manufacturer
        self.nonces = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()

    def _handler(nvr):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/xml", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _digest_ok(self) -> bool:
                h = self.headers.get("Authorization", "")
                if not h.startswith("Digest "):
                    return False
                f = dict(re.findall(r'(\w+)="?([^",]+)"?', h[7:]))
                if f.get("nonce") not in nvr.nonces or f.get("username") != nvr.user:
                    return False
                ha1 = _md5(f"{nvr.user}:{REALM}:{nvr.password}")
                ha2 = _md5(f"{self.command}:{f['uri']}")
                want = _md5(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:{f['qop']}:{ha2}")
                return want == f.get("response")

            def _challenge(self):
                n = base64.b64encode(os.urandom(12)).decode()
                nvr.nonces.add(n)
                self._send(401, b"", extra={"WWW-Authenticate": f'Digest realm="{REALM}", qop="auth", nonce="{n}", algorithm=MD5'})

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            # ---------------------------------------------------------- ONVIF
            def _wsse_ok(self, xml: str) -> bool:
                g = lambda t: (m.group(1) if (m := re.search(rf"<(?:\w+:)?{t}[^>]*>([^<]+)<", xml)) else None)
                u, pw, nonce, created = g("Username"), g("Password"), g("Nonce"), g("Created")
                if not all((u, pw, nonce, created)) or u != nvr.user:
                    return False
                want = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode()
                                                     + nvr.password.encode()).digest()).decode()
                return want == pw

            def _soap(self, inner: str):
                env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                       'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tse="http://www.onvif.org/ver10/search/wsdl" '
                       'xmlns:trp="http://www.onvif.org/ver10/replay/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
                       f"<s:Body>{inner}</s:Body></s:Envelope>")
                self._send(200, env.encode(), "application/soap+xml")

            def do_POST(self):
                body = self._body().decode("utf-8", "replace")
                if self.path.startswith("/onvif/"):
                    if not self._wsse_ok(body):
                        return self._soap("<s:Fault><s:Reason><s:Text>Sender not authorized</s:Text></s:Reason></s:Fault>")
                    base = f"http://127.0.0.1:{nvr.port}"
                    if "GetDeviceInformation" in body:
                        return self._soap(f"<tds:GetDeviceInformationResponse><tds:Manufacturer>{nvr.mfr}</tds:Manufacturer>"
                                          "<tds:Model>MOCK-NVR-4CH</tds:Model><tds:FirmwareVersion>V4.0.0</tds:FirmwareVersion>"
                                          "<tds:SerialNumber>SN-MOCK-0001</tds:SerialNumber><tds:HardwareId>1.0</tds:HardwareId>"
                                          "</tds:GetDeviceInformationResponse>")
                    if "GetSystemDateAndTime" in body:
                        n = datetime.now(timezone.utc)
                        return self._soap("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
                                          f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>"
                                          f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                                          "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
                    if "GetServices" in body:
                        svc = lambda ns, p: f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{base}{p}</tds:XAddr></tds:Service>"
                        return self._soap("<tds:GetServicesResponse>" + svc("http://www.onvif.org/ver10/search/wsdl", "/onvif/search_service")
                                          + svc("http://www.onvif.org/ver10/replay/wsdl", "/onvif/replay_service") + "</tds:GetServicesResponse>")
                    if "FindRecordings" in body:
                        return self._soap("<tse:FindRecordingsResponse><tse:SearchToken>tok-1</tse:SearchToken></tse:FindRecordingsResponse>")
                    if "GetRecordingSearchResults" in body:
                        items = "".join(f"<tt:RecordingInformation><tt:RecordingToken>rec-{k}</tt:RecordingToken><tt:Source>"
                                        f"<tt:Name>{p.stem}</tt:Name></tt:Source><tt:EarliestRecording>2026-09-28T21:14:00Z"
                                        "</tt:EarliestRecording><tt:LatestRecording>2026-09-28T21:17:00Z</tt:LatestRecording>"
                                        "<tt:RecordingStatus>Stopped</tt:RecordingStatus></tt:RecordingInformation>"
                                        for k, p in enumerate(sorted((nvr.exports / "dahua").glob("*.dav")), 1))
                        return self._soap(f"<tse:GetRecordingSearchResultsResponse><tse:ResultList><tse:SearchState>Completed"
                                          f"</tse:SearchState>{items}</tse:ResultList></tse:GetRecordingSearchResultsResponse>")
                    if "GetReplayUri" in body:
                        return self._soap(f"<trp:GetReplayUriResponse><trp:Uri>rtsp://127.0.0.1:554/replay/rec-1</trp:Uri>"
                                          "</trp:GetReplayUriResponse>")
                    return self._send(400)
                if self.path == "/ISAPI/ContentMgmt/search":
                    if not self._digest_ok():
                        return self._challenge()
                    track = int(re.search(r"<trackID>(\d+)</trackID>", body).group(1))
                    uri = f"rtsp://127.0.0.1/Streaming/tracks/{track}/?starttime=20260928T211400Z&amp;endtime=20260928T211700Z&amp;name=ch{track // 100:02d}"
                    return self._send(200, f"<CMSearchResult><matchList><searchMatchItem><mediaSegmentDescriptor><playbackURI>{uri}"
                                           "</playbackURI></mediaSegmentDescriptor></searchMatchItem></matchList></CMSearchResult>".encode())
                self._send(404)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path in ("/cgi-bin/loadfile.cgi", "/ISAPI/ContentMgmt/download"):
                    if not self._digest_ok():
                        return self._challenge()
                if u.path == "/cgi-bin/loadfile.cgi":
                    q = urllib.parse.parse_qs(u.query)
                    ch = int(q["channel"][0])
                    t0 = datetime.strptime(q["startTime"][0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                    t1 = datetime.strptime(q["endTime"][0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                    src = next((nvr.exports / "dahua").glob(f"ch{ch:02d}_*.dav")).read_bytes()
                    frames = [f for f in dhav.scan(src) if t0 <= f.date_s <= t1]
                    k = next((i for i, f in enumerate(frames) if f.keyframe), len(frames))   # start on a keyframe
                    return self._send(200, b"".join(src[f.offset:f.offset + f.length] for f in frames[k:]),
                                      "application/octet-stream")
                if u.path == "/ISAPI/ContentMgmt/download":
                    body = self._body().decode()
                    ch = int(re.search(r"name=ch(\d+)", body).group(1))
                    return self._send(200, next((nvr.exports / "hikvision").glob(f"ch{ch:02d}_*.ps")).read_bytes(),
                                      "application/octet-stream")
                self._send(404)
        return H
