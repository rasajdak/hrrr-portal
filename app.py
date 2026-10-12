"""
HRRR Interpretation Portal — Flask backend.

Endpoints
  GET /                        -> static/index.html
  GET /api/config?model=      -> models, that model's fields and newest run, regions
  GET /api/overlay            -> rendered field PNG (query: model, field, run, fxx)
  GET /api/map                -> finished static map PNG (adds: region)
  GET /share                  -> a page around one static map, for posting a link
  GET /api/legend?field=...   -> colorbar PNG for a field
  GET /api/panel?lat=&lon=    -> every field a desk panel shows, in one object
  POST /api/purpleair?key=    -> relay a PurpleAir Data Processor push to Pelion

Run:  python app.py           (dev server on http://127.0.0.1:8000)
"""

import os
import traceback
import urllib.error
import urllib.request
from html import escape
from datetime import datetime, timezone

from flask import Flask, request, send_file, jsonify, Response, send_from_directory

import hrrr_render as hr
import hrrr_panel

# WAQI air-quality tile token — read from the environment so it never lives in
# the repo or the client. Set on the server (systemd Environment=WAQI_TOKEN=...).
WAQI_TOKEN = os.environ.get("WAQI_TOKEN", "")

app = Flask(__name__, static_folder="static", static_url_path="")


def _model():
    m = request.args.get("model", "hrrr")
    if m not in hr.MODELS:
        raise ValueError("unknown model %r" % m)
    return m


def _parse_run(s, model="hrrr"):
    """'2026080112' (YYYYMMDDHH) -> aware UTC datetime; default = newest on AWS."""
    if not s:
        return hr.latest_available_run(model)
    try:
        dt = datetime.strptime(s, "%Y%m%d%H")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("run must be YYYYMMDDHH, got %r" % s)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config")
def config():
    try:
        model = _model()
    except ValueError as e:
        return jsonify(error=str(e)), 400
    run = hr.latest_available_run(model)
    m = hr.MODELS[model]
    return jsonify(
        model=model,
        models=hr.model_catalog(),
        regions=hr.region_catalog(),
        fields=hr.field_catalog(model),
        bounds=hr.extent_bounds(),
        latest_run=run.strftime("%Y%m%d%H"),
        latest_run_label=run.strftime("%Y-%m-%d %HZ"),
        run_iso=run.strftime("%Y-%m-%dT%H:00:00Z"),
        max_fxx=m["max_fxx"],
        step_hours=m["step"],
        render_rev=hr.RENDER_REV,
    )


@app.route("/api/point")
def point():
    field = request.args.get("field", "refc")
    try:
        model = _model()
        run = _parse_run(request.args.get("run"), model)
        fxx = int(request.args.get("fxx", "0"))
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        f = hr.spec(field, model)
        val = hr.sample(field, run, fxx, lat, lon, model)
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("point failed: %s", e)
        return jsonify(error="sample failed: %s" % e), 502
    return jsonify(field=field, fxx=fxx, value=val, unit=f["unit"], label=f["label"])


@app.route("/api/panel")
def panel():
    """
    The whole of a small screen's worth of forecast for one point.

    A device with a display cannot sensibly make thirty calls to /api/point, so
    this answers the lot at once and is cached per run and point. Pass wait=0 to
    get a 202 rather than sit through a cold build.
    """
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify(error="lat and lon are required"), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify(error="lat/lon out of range"), 400

    wait = request.args.get("wait", "1") != "0"
    try:
        payload, status = hrrr_panel.get(lat, lon, wait=wait)
    except Exception as e:
        app.logger.error("panel failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="panel failed: %s" % e), 502

    resp = jsonify(payload)
    # Read-only public forecast numbers, and the things that want them live on
    # other hosts: the lake panel page is served from ryansajdak.com and the
    # ESP32 panel is not a browser at all. Nothing here is user-specific and
    # nothing is written, so there is nothing for an origin check to protect.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, status


@app.route("/api/overlay")
def overlay():
    field = request.args.get("field", "refc")
    try:
        model = _model()
        run = _parse_run(request.args.get("run"), model)
        fxx = int(request.args.get("fxx", "0"))
        path = hr.render(field, run, fxx, model)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:                       # fetch/render failure
        app.logger.error("overlay failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="render failed: %s" % e), 502
    resp = send_file(path, mimetype="image/png", max_age=3600)
    return resp


def _map_args():
    """(model, field, run, fxx, region) for the static map routes."""
    model = _model()
    field = request.args.get("field", "refc")
    region = request.args.get("region", "conus")
    run = _parse_run(request.args.get("run"), model)
    fxx = int(request.args.get("fxx", "0"))
    return model, field, run, fxx, region


@app.route("/api/map")
def static_map():
    """A finished map: borders, title, city values, colorbar. Made to be posted."""
    try:
        model, field, run, fxx, region = _map_args()
        path = hr.render_map(field, run, fxx, region, model)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("map failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="render failed: %s" % e), 502
    name = "%s_%s_%s_f%03d_%s.png" % (model, field, run.strftime("%Y%m%d%H"), fxx, region)
    resp = send_file(path, mimetype="image/png", max_age=86400,
                     as_attachment=request.args.get("dl") == "1", download_name=name)
    return resp


@app.route("/share")
def share():
    """
    One static map on a page of its own. A bare PNG link posts as a bare link;
    this carries the Open Graph tags that make Facebook and the rest show the
    map itself. The run is pinned in the URL, so the picture never changes
    under a post.
    """
    try:
        model, field, run, fxx, region = _map_args()
        f = hr.spec(field, model)
        if region not in hr.REGIONS:
            raise ValueError("unknown region %r" % region)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    q = "model=%s&field=%s&run=%s&fxx=%d&region=%s" % (
        model, field, run.strftime("%Y%m%d%H"), fxx, region)
    root = request.url_root.replace("http://", "https://")
    img = "%sapi/map?%s" % (root, q)
    title = escape("%s %s, %s" % (hr.MODELS[model]["name"], f["label"],
                                  hr.REGIONS[region]["label"]))
    desc = escape(hr._when(run, fxx, hr.REGIONS[region]["tz"]).replace("  ·  ", ", "))
    return Response(
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>%(t)s</title>"
        "<meta property='og:type' content='website'>"
        "<meta property='og:title' content='%(t)s'>"
        "<meta property='og:description' content='%(d)s'>"
        "<meta property='og:image' content='%(i)s'>"
        "<meta property='og:image:width' content='%(w)d'>"
        "<meta property='og:image:height' content='%(h)d'>"
        "<meta name='twitter:card' content='summary_large_image'>"
        "<style>body{margin:0;background:#0b1017;color:#e8eef6;font-family:-apple-system,"
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;text-align:center}"
        "img{max-width:100%%;height:auto;display:block;margin:0 auto}"
        "p{font-size:14px;margin:14px}a{color:#4ea3ff}</style>"
        "<img src='%(i)s' alt='%(t)s'>"
        "<p>%(d)s</p><p><a href='/'>Open the live map</a></p></html>"
        % dict(t=title, d=desc, i=escape(img), w=hr.MAP_W, h=hr.MAP_H),
        mimetype="text/html")


@app.route("/api/legend")
def legend():
    field = request.args.get("field", "refc")
    try:
        png = hr.render_legend(field, _model())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/tiles/aqi/<int:z>/<int:x>/<int:y>.png")
def aqi_tile(z, x, y):
    """Proxy WAQI AQI tiles so the token stays server-side (never in the client)."""
    if not WAQI_TOKEN:
        return jsonify(error="AQI token not configured on server"), 503
    url = "https://tiles.waqi.info/tiles/usepa-aqi/%d/%d/%d.png?token=%s" % (z, x, y, WAQI_TOKEN)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hrrr-portal"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "image/png")
    except Exception as e:
        app.logger.error("aqi tile failed: %s", e)
        return jsonify(error="aqi fetch failed"), 502
    return Response(data, mimetype=ctype,
                    headers={"Cache-Control": "public, max-age=600"})


# PurpleAir Data Processor relay for the Pelion Field Station air API.
#
# PurpleAir sensors POST their reading with Transfer-Encoding: chunked, and the
# mrsajdak.com host (Bluehost) runs ModSecurity that answers every chunked
# request with a 406, with no cPanel switch to turn it off. nginx here buffers
# the chunked body, so by the time Flask sees it it is a plain byte string,
# which we pass on with a Content-Length. The key rides through untouched in
# the query string and in whatever header PurpleAir's "API Key" field uses.
PELION_AIR = "https://mrsajdak.com/pelionscience/api/air.php"
_HOP = {"host", "content-length", "transfer-encoding", "connection",
        "user-agent", "accept-encoding", "keep-alive", "expect"}


@app.route("/api/purpleair", methods=["GET"])
def purpleair_about():
    # School web filters (WNYRIC) fetch a URL themselves before deciding to let
    # a device reach it. Give them a plain, obviously educational page.
    return Response(
        "<!doctype html><title>Pelion Field Station data relay</title>"
        "<h1>Pelion Field Station air quality data relay</h1>"
        "<p>City Honors School, Buffalo Public Schools. Student environmental science "
        "monitoring: PurpleAir air quality sensors report here, and readings are published at "
        '<a href="https://mrsajdak.com/pelionscience/#air">mrsajdak.com/pelionscience</a>.</p>',
        mimetype="text/html")


@app.route("/api/purpleair", methods=["POST"])
def purpleair_relay():
    body = request.get_data(cache=False)
    if not body:
        return jsonify(error="empty body"), 400
    url = PELION_AIR + ("?" + request.query_string.decode() if request.query_string else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers["User-Agent"] = "pelion-purpleair-relay"
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return Response(r.read(), status=r.status, mimetype="application/json")
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code, mimetype="application/json")
    except Exception as e:
        app.logger.error("purpleair relay failed: %s", e)
        return jsonify(error="relay failed"), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="127.0.0.1", port=port, debug=True)
