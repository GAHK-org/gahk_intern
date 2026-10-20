// Køkkenvagter: the balance forecast on Min køkkenkonto (Chart.js, tree-shaken). Data comes from the
// `kitchen-forecast-data` json_script rendered by kitchen.views._forecast_context; nothing runs on
// pages without it. The balance moves in jumps (a shift ends, the 1st of a month), so it is a step
// line over the green / yellow / red zones, with the alarm zone under -6 hatched in deeper red.
import {
  Chart,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
  type Plugin,
  type ScriptableContext,
} from "chart.js";

Chart.register(LineController, LineElement, PointElement, LinearScale, Tooltip);

// `kind` marks the points that are not a booking: today, where the binding 30 days end, and the end
// of the calendar.
type Point = { x: number; y: number; items: [string, number][]; kind?: "now" | "outlook" | "end" };
type Forecast = { points: Point[]; yellowMax: number; alarmBelow: number; outlookAt: number };
type Zone = "good" | "warn" | "neg" | "alarm";

const weekdayDate = new Intl.DateTimeFormat("da-DK", { weekday: "short", day: "numeric", month: "long" });
const monthName = new Intl.DateTimeFormat("da-DK", { month: "short" });
const signed = (n: number) => (n > 0 ? `+${n}` : `${n}`);

function render(canvas: HTMLCanvasElement, data: Forecast) {
  const css = getComputedStyle(canvas);
  const token = (name: string) => css.getPropertyValue(name).trim();
  const ink = token("--ink") || "#1A231D";
  const muted = token("--muted") || "#616B62";
  const grid = token("--border") || "#E7E4DB";
  const surface = token("--surface") || "#FFFFFF";
  const color: Record<Zone, string> = {
    good: token("--zone-good"),
    warn: token("--zone-warn"),
    neg: token("--zone-neg"),
    alarm: token("--zone-alarm"),
  };

  // Balances are whole points, so the zone edges sit half-way between them: a line resting on 0 is
  // plainly in yellow and one on -1 plainly in red, rather than on a boundary.
  const { yellowMax, alarmBelow, outlookAt } = data;
  const zone = (y: number): Zone =>
    y > yellowMax ? "good" : y >= 0 ? "warn" : y >= alarmBelow ? "neg" : "alarm";

  const ys = data.points.map((p) => p.y);
  const yMin = Math.min(...ys, 0) - 2;
  const yMax = Math.max(...ys, yellowMax) + 2;
  const first = data.points[0].x;
  const last = data.points[data.points.length - 1].x;

  // The 1st of each month is when the charge lands, so those are the x ticks and gridlines.
  const monthTicks: number[] = [];
  const d = new Date(first);
  d.setDate(1);
  d.setHours(0, 0, 0, 0);
  for (d.setMonth(d.getMonth() + 1); d.getTime() <= last; d.setMonth(d.getMonth() + 1)) {
    monthTicks.push(d.getTime());
  }

  const hatch = (() => {
    const tile = document.createElement("canvas");
    tile.width = tile.height = 8;
    const t = tile.getContext("2d");
    if (!t) return token("--zone-alarm-fill");
    t.fillStyle = token("--zone-alarm-fill");
    t.fillRect(0, 0, 8, 8);
    t.strokeStyle = token("--zone-alarm-hatch");
    t.lineWidth = 2;
    t.beginPath();
    for (const o of [-8, 0, 8]) {
      t.moveTo(o, 8);
      t.lineTo(o + 8, 0);
    }
    t.stroke();
    return t.createPattern(tile, "repeat") ?? token("--zone-alarm-fill");
  })();

  const zones: Plugin<"line"> = {
    id: "kitchenZones",
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea: area, scales } = chart;
      const y = scales.y;
      const band = (from: number, to: number, fill: string | CanvasPattern, label: string, strong = false) => {
        const top = Math.max(area.top, Math.min(area.bottom, y.getPixelForValue(to)));
        const bottom = Math.max(area.top, Math.min(area.bottom, y.getPixelForValue(from)));
        if (bottom - top < 1) return;
        ctx.fillStyle = fill;
        ctx.fillRect(area.left, top, area.right - area.left, bottom - top);
        // The zone's name in its own band, so the colour never has to carry the meaning alone.
        if (bottom - top >= 16) {
          ctx.font = `${strong ? "700" : "500"} 11px system-ui, sans-serif`;
          ctx.fillStyle = strong ? color.alarm : muted;
          ctx.textAlign = "right";
          ctx.textBaseline = "top";
          ctx.fillText(label, area.right - 6, top + 4);
        }
      };
      ctx.save();
      band(yellowMax + 0.5, y.max, token("--zone-good-fill"), "Godt");
      band(-0.5, yellowMax + 0.5, token("--zone-warn-fill"), "Tæt på 0");
      band(alarmBelow - 0.5, -0.5, token("--zone-neg-fill"), "I minus");
      band(y.min, alarmBelow - 0.5, hatch, "Langt i minus", true);
      // Past the binding 30 days, shifts can still be unenrolled: a veil over the zones and a marked
      // line say that part is tentative (the forecast line itself turns dashed there).
      const x = chart.scales.x.getPixelForValue(outlookAt);
      if (x > area.left && x < area.right) {
        ctx.fillStyle = surface;
        ctx.globalAlpha = 0.35;
        ctx.fillRect(x, area.top, area.right - x, area.bottom - area.top);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = muted;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
        ctx.stroke();
        ctx.font = "600 11px system-ui, sans-serif";
        ctx.fillStyle = muted;
        ctx.textBaseline = "top";
        ctx.textAlign = "right";
        ctx.fillText("bundet", x - 6, area.top + 4);
        ctx.textAlign = "left";
        ctx.fillText("foreløbigt", x + 6, area.top + 4);
      }
      ctx.restore();
    },
  };

  // A hairline at the hovered step: readers aim at a date, not at a 2px line.
  const crosshair: Plugin<"line"> = {
    id: "kitchenCrosshair",
    afterDatasetsDraw(chart) {
      const active = chart.tooltip?.getActiveElements() ?? [];
      if (!active.length) return;
      const { ctx, chartArea: area } = chart;
      const x = active[0].element.x;
      ctx.save();
      ctx.strokeStyle = muted;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  const isMarker = (i: number) => data.points[i].kind === "outlook" || data.points[i].kind === "end";
  const pointZone = (ctx: ScriptableContext<"line">) => zone((ctx.raw as Point).y);

  new Chart(canvas, {
    type: "line",
    data: {
      datasets: [
        {
          data: data.points,
          parsing: false,
          // Room for the markers at the edges (today, the horizon) instead of cutting them in half.
          clip: 8,
          stepped: "before",
          borderColor: ink,
          borderWidth: 2,
          // In the alarm zone the line itself turns red and heavier; past the binding 30 days it is
          // dashed.
          segment: {
            borderColor: (ctx) => (zone(ctx.p0.parsed.y ?? 0) === "alarm" ? color.alarm : undefined),
            borderWidth: (ctx) => (zone(ctx.p0.parsed.y ?? 0) === "alarm" ? 3 : undefined),
            borderDash: (ctx) => ((ctx.p0.parsed.x ?? 0) >= outlookAt ? [6, 4] : undefined),
          },
          pointRadius: (ctx) => (isMarker(ctx.dataIndex) ? 0 : pointZone(ctx) === "alarm" ? 6 : 4),
          pointHoverRadius: (ctx) => (pointZone(ctx) === "alarm" ? 8 : 6),
          pointHitRadius: 12,
          pointBackgroundColor: (ctx) => color[pointZone(ctx)],
          pointBorderColor: surface,
          pointBorderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      layout: { padding: { top: 4, right: 8, left: 4 } },
      scales: {
        x: {
          type: "linear",
          min: first,
          max: last,
          afterBuildTicks: (axis) => {
            axis.ticks = monthTicks.map((value) => ({ value }));
          },
          ticks: {
            color: muted,
            autoSkip: false,
            maxRotation: 0,
            callback: (value) => `1. ${monthName.format(new Date(Number(value)))}`,
          },
          grid: { color: grid },
          border: { display: false },
        },
        y: {
          min: yMin,
          max: yMax,
          ticks: { color: muted, precision: 0, includeBounds: false, callback: (value) => signed(Number(value)) },
          grid: {
            color: (ctx) => (ctx.tick.value === 0 ? muted : grid),
            lineWidth: (ctx) => (ctx.tick.value === 0 ? 1.5 : 1),
          },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: surface,
          borderColor: grid,
          borderWidth: 1,
          titleColor: muted,
          titleFont: { weight: "normal" },
          bodyColor: ink,
          footerColor: muted,
          footerFont: { weight: "normal" },
          padding: 10,
          displayColors: false,
          callbacks: {
            title: (items) => {
              const point = items[0].raw as Point;
              if (point.kind === "now") return "I dag";
              const date = weekdayDate.format(point.x);
              if (point.kind === "outlook") return `${date} · de bundne 30 dage slutter`;
              if (point.kind === "end") return `${date} · kalenderen slutter`;
              return point.x > outlookAt ? `${date} · foreløbigt` : date;
            },
            label: (item) => `Saldo ${(item.raw as Point).y} point`,
            footer: (items) =>
              (items[0].raw as Point).items.map(([label, amount]) => `${signed(amount)}  ${label}`),
          },
          bodyFont: { weight: "bold", size: 14 },
        },
      },
    },
    plugins: [zones, crosshair],
  });
}

const node = document.getElementById("kitchen-forecast-data");
const canvas = document.getElementById("kitchen-forecast");
if (node && canvas instanceof HTMLCanvasElement) {
  render(canvas, JSON.parse(node.textContent || "{}") as Forecast);
}
