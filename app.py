"""
HRRR Interpretation Portal — Flask backend.

Endpoints
  GET /                        -> static/index.html
  GET /api/config?model=      -> models, that model's fields and newest run, regions
  GET /api/overlay            -> rendered field PNG (query: model, field, run, fxx)
  GET /api/map                -> finished static map PNG (adds: region, shape)
  GET /api/ready?model=&run=  -> which static maps of that run are already drawn
  GET /api/runs?model=        -> recent runs, newest first, for the run picker
  POST /api/save              -> keep one static map for good, under /saved/
  GET /saved                  -> the kept maps, newest first
  GET /api/posted             -> the last forecast hour of a run the model has posted (query: model, run)
  GET /loop                   -> the animated futurecast page
  GET /api/loop               -> its run, frame times and layers (query: model, region, run, kind)
  GET /api/loop/frame         -> one time's weather as a data PNG (adds: run, m; kind radar|temp|gust)
  GET /api/loop/base          -> a region's base or label layer (query: region, layer)
  GET /share                  -> a page around one static map, for posting a link
  GET /api/legend?field=...   -> colorbar PNG for a field
  GET /api/panel?lat=&lon=    -> every field a desk panel shows, in one object
  POST /api/purpleair?key=    -> relay a PurpleAir Data Processor push to Pelion

Run:  python app.py           (dev server on http://127.0.0.1:8000)
"""

import os
import re
import shutil
import traceback
import urllib.error
import urllib.request
from html import escape
from datetime import datetime, timedelta, timezone

from flask import Flask, request, send_file, jsonify, Response, send_from_directory

import hrrr_render as hr
import hrrr_panel
import loop

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
        max_fxx=hr.run_max(model, run),
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


def _shape():
    shape = request.args.get("shape", "wide")
    if shape not in hr.SHAPES:
        raise ValueError("shape is one of %s" % ", ".join(hr.SHAPES))
    return shape


def _map_args():
    """(model, field, run, fxx, region) for the static map routes."""
    model = _model()
    field = request.args.get("field", "refc")
    region = request.args.get("region", "conus")
    run = _parse_run(request.args.get("run"), model)
    fxx = int(request.args.get("fxx", "0"))
    return model, field, run, fxx, region


@app.route("/loop")
def loop_page():
    return send_from_directory(app.static_folder, "loop.html")


@app.route("/api/loop")
def loop_meta():
    try:
        model = request.args.get("model", "hrrr")
        asked = request.args.get("run")
        return jsonify(loop.meta(model, request.args.get("region", "wny"),
                                 _parse_run(asked, model) if asked else None,
                                 request.args.get("kind", "radar")))
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/loop/frame")
def loop_frame():
    try:
        model = _model()
        if model not in loop.LOOPS:
            raise ValueError("no loop for %r" % model)
        region = request.args.get("region", "wny")
        hr.region_parts(region)                 # 'name' or 'name@tall', else ValueError
        run = _parse_run(request.args.get("run"), model) if request.args.get("run") else loop.latest_run(model)
        path = loop.frame_png(model, run, int(request.args.get("m", "0")), region,
                              request.args.get("kind", "radar"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("loop frame failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="frame failed: %s" % e), 502
    return send_file(path, mimetype="image/png", max_age=86400)


@app.route("/api/loop/flow")
def loop_flow():
    """How the weather moves from the frame at m minutes to the next one."""
    try:
        model = _model()
        if model not in loop.LOOPS:
            raise ValueError("no loop for %r" % model)
        region = request.args.get("region", "wny")
        hr.region_parts(region)                 # 'name' or 'name@tall', else ValueError
        run = _parse_run(request.args.get("run"), model) if request.args.get("run") else loop.latest_run(model)
        path = loop.flow_png(model, run, int(request.args.get("m", "0")), region)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("loop flow failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="flow failed: %s" % e), 502
    return send_file(path, mimetype="image/png", max_age=86400)


@app.route("/api/loop/base")
def loop_base():
    try:
        path = loop.base_png(request.args.get("region", "wny"), request.args.get("layer", "base"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("loop base failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="layer failed: %s" % e), 502
    return send_file(path, mimetype="image/png", max_age=86400)


@app.route("/api/runs")
def runs():
    """Recent runs of a model, newest first, for the page's run picker."""
    try:
        return jsonify(model=_model(), runs=hr.recent_runs(_model()))
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/posted")
def posted():
    """How far into a run the model has posted, so the page can grey out the rest."""
    try:
        model = _model()
        run = _parse_run(request.args.get("run"), model)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    last = hr.posted_through(model, run)
    return jsonify(model=model, run=run.strftime("%Y%m%d%H"), through=last,
                   max_fxx=hr.run_max(model, run), complete=last >= hr.run_max(model, run))


@app.route("/api/ready")
def ready():
    """
    Which static maps of a run are already drawn (see hr.ready_maps).

    With no run given, answers for the best-stocked recent run rather than the
    newest: a run counts as newest the moment it starts landing, well before
    its maps are drawn, and a page that keeps to ready maps would open on
    nothing. The newest run wins once it has half as many maps as the one
    before it.
    """
    try:
        model = _model()
        asked = request.args.get("run")
        run = _parse_run(asked, model)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    got = hr.ready_maps(model, run)
    latest = run
    if not asked:
        count = lambda r: sum(len(h) for f in r.values() for h in f.values())
        cycle = timedelta(hours=hr.MODELS[model]["cycle"])
        older = [(run - cycle * k, hr.ready_maps(model, run - cycle * k)) for k in (1, 2)]
        best = max(count(r) for _, r in older)
        if count(got) < 0.5 * best:
            run, got = next((d, r) for d, r in older if count(r) >= 0.5 * best)
    return jsonify(model=model, run=run.strftime("%Y%m%d%H"), run_label=run.strftime("%Y-%m-%d %HZ"),
                   latest_run=latest.strftime("%Y%m%d%H"), ready=got)


@app.route("/api/map")
def static_map():
    """A finished map: borders, title, city values, colorbar. Made to be posted."""
    try:
        model, field, run, fxx, region = _map_args()
        shape = _shape()
        path = hr.render_map(field, run, fxx, region, model, shape)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("map failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="render failed: %s" % e), 502
    name = "%s_%s_%s_f%03d_%s%s.png" % (model, field, run.strftime("%Y%m%d%H"), fxx, region,
                                        "" if shape == "wide" else "_" + shape)
    resp = send_file(path, mimetype="image/png", max_age=86400,
                     as_attachment=request.args.get("dl") == "1", download_name=name)
    return resp


# Kept maps. The render cache is keyed by hash and free to be emptied; a map
# someone chose to keep is copied here under a name that says what it is, and
# its URL then never changes or expires.
SAVED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved")
SAVED_MAX = 500
_SAVED_NAME = re.compile(r"^([a-z]+)_([a-z0-9_]+)_(\d{10})_f(\d{3})_([a-z]+?)(?:_(square|tall))?\.png$")


@app.route("/api/save", methods=["POST"])
def save_map():
    try:
        model, field, run, fxx, region = _map_args()
        shape = _shape()
        path = hr.render_map(field, run, fxx, region, model, shape)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        app.logger.error("save failed: %s\n%s", e, traceback.format_exc())
        return jsonify(error="render failed: %s" % e), 502
    os.makedirs(SAVED_DIR, exist_ok=True)
    name = "%s_%s_%s_f%03d_%s%s.png" % (model, field, run.strftime("%Y%m%d%H"), fxx, region,
                                        "" if shape == "wide" else "_" + shape)
    dest = os.path.join(SAVED_DIR, name)
    if not os.path.exists(dest):
        # anyone can press Save, so the shelf has an end
        if len(os.listdir(SAVED_DIR)) >= SAVED_MAX:
            return jsonify(error="the saved shelf is full (%d maps)" % SAVED_MAX), 507
        shutil.copyfile(path, dest + ".part")
        os.replace(dest + ".part", dest)
    return jsonify(name=name, url="/saved/" + name)


@app.route("/saved/<name>")
def saved_file(name):
    if not _SAVED_NAME.match(name):
        return jsonify(error="no such map"), 404
    return send_from_directory(SAVED_DIR, name, mimetype="image/png", max_age=86400)


@app.route("/saved")
@app.route("/saved/")
def saved_index():
    rows = []
    if os.path.isdir(SAVED_DIR):
        names = [n for n in os.listdir(SAVED_DIR) if _SAVED_NAME.match(n)]
        names.sort(key=lambda n: os.path.getmtime(os.path.join(SAVED_DIR, n)), reverse=True)
        for n in names:
            model, field, run, fxx, region, _shape_ = _SAVED_NAME.match(n).groups()
            try:
                what = "%s %s, %s" % (hr.MODELS[model]["name"], hr.spec(field, model)["label"],
                                      hr.REGIONS[region]["label"])
                when = hr._when(datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc),
                                int(fxx), hr.REGIONS[region]["tz"]).replace("  ·  ", ", ")
            except (KeyError, ValueError):
                what, when = n, ""
            rows.append("<a href='/saved/%s'><img loading=lazy src='/saved/%s' alt='%s'>"
                        "<b>%s</b><span>%s</span></a>" % (n, n, escape(what), escape(what), escape(when)))
    return Response(
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'><title>Saved maps</title>"
        "<link rel=stylesheet href='https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600"
        "&family=Barlow+Condensed:wght@600&display=swap'>"
        "<style>body{margin:0;padding:18px;background:#0b1017;color:#e8eef6;font-family:Barlow,"
        "Helvetica,Arial,sans-serif}h1{font:600 30px 'Barlow Condensed',Barlow,sans-serif;margin:0 0 4px}"
        "p{color:#8ea3bd;margin:0 0 16px;font-size:14px}p a{color:#4ea3ff}"
        ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}"
        ".g a{color:inherit;text-decoration:none;background:#131b26;border:1px solid #243244;"
        "border-radius:10px;overflow:hidden;display:block}.g img{width:100%%;aspect-ratio:16/10;"
        "display:block;background:#fff}.g b{display:block;padding:9px 11px 0;font-weight:600;font-size:15px}"
        ".g span{display:block;padding:2px 11px 10px;color:#8ea3bd;font-size:13px}</style>"
        "<h1>Saved maps</h1><p>%d kept. <a href='/'>Back to the maps</a></p><div class=g>%s</div></html>"
        % (len(rows), "".join(rows) or "<p>Nothing saved yet.</p>"),
        mimetype="text/html")


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
    shape = request.args.get("shape", "wide")
    if shape not in hr.SHAPES:
        shape = "wide"
    q = "model=%s&field=%s&run=%s&fxx=%d&region=%s%s" % (
        model, field, run.strftime("%Y%m%d%H"), fxx, region, "" if shape == "wide" else "&shape=" + shape)
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
        "<p>%(d)s</p><p><a href='https://ryansajdak.com/'>ryansajdak.com</a></p></html>"
        % dict(t=title, d=desc, i=escape(img), w=hr.SHAPES[shape]["w"], h=hr.SHAPES[shape]["h"]),
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
