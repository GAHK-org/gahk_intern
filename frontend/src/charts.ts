// Interactive charts for /intern/statistik/ (Chart.js, tree-shaken). Data comes from a
// `json_script` block rendered by the stats view; nothing runs on pages without it.
import {
  ArcElement,
  CategoryScale,
  Chart,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PieController,
  PointElement,
  Tooltip,
} from "chart.js";

Chart.register(
  PieController, ArcElement, LineController, LineElement, PointElement,
  LinearScale, CategoryScale, Tooltip, Legend, Filler,
);

// Validated categorical palette (dataviz reference, light surface), fixed order — never cycled.
const CATEGORICAL = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"];
const INK = "#0b0b0b";
const MUTED = "#898781";
const GRID = "#e1e0d9";
const SURFACE = "#ffffff";
const ACCENT = "#2a78d6";

Chart.defaults.font.family =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
Chart.defaults.color = MUTED;

type Series = { labels: string[]; data: number[] };

function pie(id: string, spec: Series | undefined) {
  const el = document.getElementById(id) as HTMLCanvasElement | null;
  if (!el || !spec || !spec.data.length) return;
  new Chart(el, {
    type: "pie",
    data: {
      labels: spec.labels,
      datasets: [{ data: spec.data, backgroundColor: CATEGORICAL, borderColor: SURFACE, borderWidth: 2 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "right", labels: { color: INK, boxWidth: 12, padding: 10 } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const total = (ctx.dataset.data as number[]).reduce((a, b) => a + b, 0) || 1;
              const v = ctx.parsed as number;
              return ` ${ctx.label}: ${v} (${Math.round((v / total) * 100)}%)`;
            },
          },
        },
      },
    },
  });
}

function timeseries(id: string, spec: Series | undefined) {
  const el = document.getElementById(id) as HTMLCanvasElement | null;
  if (!el || !spec || !spec.data.length) return;
  new Chart(el, {
    type: "line",
    data: {
      labels: spec.labels,
      datasets: [
        {
          label: "Besøg",
          data: spec.data,
          borderColor: ACCENT,
          backgroundColor: "rgba(42,120,214,0.12)",
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 5,
          fill: true,
          tension: 0.25,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: MUTED, maxTicksLimit: 8, autoSkip: true } },
        y: { beginAtZero: true, grid: { color: GRID }, ticks: { color: MUTED, precision: 0 } },
      },
    },
  });
}

const node = document.getElementById("stats-data");
if (node) {
  const d = JSON.parse(node.textContent || "{}");
  pie("chart-applications", d.applications);
  pie("chart-heard", d.heard);
  timeseries("chart-visits", d.visits);
}
