"""
matrix_view.py — Render the value & trend matrix as an interactive 3D graph.

Uses the 3d-force-graph library (Three.js/WebGL) inside a Streamlit HTML
component: real 3D spheres you drag to rotate, scroll to zoom, hover to light up
a concept's connections, and click to read its value/heat/tier. Sphere size =
value/prominence, colour = market heat (cool→hot), halo ring = sale tier.
"""

from __future__ import annotations

import json


HEAT_STOPS = [  # cool → hot
    (0.0, "#3a6ea5"), (0.25, "#57b894"), (0.5, "#e8c34a"),
    (0.75, "#e8963a"), (1.0, "#d64545"),
]
TIER_RING = {
    "Weekly Estate": "#57b894",
    "Classics Collection": "#c88ce0",
}
DEFAULT_RING = "#e8c34a"   # Special sales (stamps / wine / vinyl / …)


def _heat_color(h: float) -> str:
    h = max(0.0, min(1.0, h))
    for i in range(len(HEAT_STOPS) - 1):
        (a, ca), (b, cb) = HEAT_STOPS[i], HEAT_STOPS[i + 1]
        if a <= h <= b:
            t = (h - a) / (b - a or 1)
            ca = [int(ca[j:j + 2], 16) for j in (1, 3, 5)]
            cb = [int(cb[j:j + 2], 16) for j in (1, 3, 5)]
            rgb = [round(ca[k] + (cb[k] - ca[k]) * t) for k in range(3)]
            return "#%02x%02x%02x" % tuple(rgb)
    return HEAT_STOPS[-1][1]


def build_html(graph: dict, *, auto_rotate: bool = True, height: int = 720) -> str:
    """graph = {'nodes':[{id,group,count,avg_value,heat,tier}], 'links':[...]}"""
    for n in graph.get("nodes", []):
        n["color"] = _heat_color(n.get("heat", 0.5))
        n["ring"] = TIER_RING.get(n.get("tier", ""), DEFAULT_RING)
        n["val"] = max(1, n.get("count", 1))
    data = json.dumps(graph)
    rot = "true" if auto_rotate else "false"
    return f"""
<div id="wrap" style="width:100%;height:{height}px;background:#0d0d10;border-radius:14px;position:relative;overflow:hidden;">
  <div id="graph"></div>
  <div id="info" style="position:absolute;left:16px;bottom:16px;max-width:320px;
       background:#17171cdd;border:1px solid #2a2a30;border-radius:12px;padding:14px 16px;
       color:#fff;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;display:none;">
    <div id="i_name" style="font-size:17px;font-weight:800;"></div>
    <div id="i_meta" style="font-size:12px;color:#9a9aa2;margin-top:2px;"></div>
    <div id="i_val" style="font-size:13px;margin-top:8px;"></div>
    <div id="i_heat" style="font-size:13px;margin-top:4px;"></div>
  </div>
  <div style="position:absolute;right:16px;top:16px;background:#17171ccc;border:1px solid #2a2a30;
       border-radius:10px;padding:10px 12px;color:#cfcfd6;font:11px -apple-system,Segoe UI,Arial;">
    <div style="font-weight:700;color:#fff;margin-bottom:4px;">Market heat</div>
    <div style="height:8px;width:180px;border-radius:5px;background:linear-gradient(90deg,#3a6ea5,#57b894,#e8c34a,#e8963a,#d64545);"></div>
    <div style="display:flex;justify-content:space-between;"><span>soft</span><span>hot</span></div>
    <div style="margin-top:8px;"><span style="color:#57b894;">●</span> Weekly &nbsp;
      <span style="color:#c88ce0;">●</span> Classics &nbsp;
      <span style="color:#e8c34a;">●</span> Special</div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.155.0/three.min.js"></script>
<script src="https://unpkg.com/3d-force-graph@1.73.0/dist/3d-force-graph.min.js"></script>
<script>
  const data = {data};
  const el = document.getElementById('graph');
  const info = document.getElementById('info');
  const Graph = ForceGraph3D()(el)
    .backgroundColor('#0d0d10')
    .graphData(data)
    .nodeLabel(n => n.id)
    .nodeVal(n => n.val)
    .nodeColor(n => n.color)
    .nodeOpacity(0.92)
    .linkColor(() => 'rgba(140,150,170,0.25)')
    .linkWidth(l => Math.min(3, (l.weight||1) * 0.4))
    .linkDirectionalParticles(0)
    .width(el.clientWidth).height({height});
  // tier ring: draw a slightly larger translucent shell in the tier colour
  Graph.nodeThreeObject(n => {{
    const grp = new THREE.Group();
    const core = new THREE.Mesh(
      new THREE.SphereGeometry(Math.cbrt(n.val)*3, 16, 16),
      new THREE.MeshLambertMaterial({{color: n.color, transparent:true, opacity:0.95}}));
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(Math.cbrt(n.val)*3.6, 16, 16),
      new THREE.MeshBasicMaterial({{color: n.ring, transparent:true, opacity:0.18}}));
    grp.add(core); grp.add(halo);
    return grp;
  }});
  Graph.onNodeClick(n => {{
    info.style.display='block';
    document.getElementById('i_name').textContent = n.id;
    document.getElementById('i_meta').textContent = n.group + ' · ' + n.tier;
    document.getElementById('i_val').innerHTML = 'Avg value <b>$'+Math.round(n.avg_value)+'</b> · seen in <b>'+n.count+'</b> lots';
    const hpct = Math.round(n.heat*100);
    document.getElementById('i_heat').innerHTML = 'Market heat <b>'+hpct+'%</b>';
  }});
  const controls = Graph.controls();
  controls.autoRotate = {rot};
  controls.autoRotateSpeed = 0.6;
  el.addEventListener('mousedown', () => {{ controls.autoRotate = false; }});
  window.addEventListener('resize', () => Graph.width(el.clientWidth));
</script>
"""
