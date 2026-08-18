(() => {
  "use strict";

  const app = document.querySelector("#app");
  const modalRoot = document.querySelector("#modal-root");
  const toastRoot = document.querySelector("#toast-root");
  const routes = ["overview", "stands", "blueprints", "checks", "ipam", "infrastructure"];
  const VKLVIKL_BOOTSTRAP_SCRIPT = `#!/usr/bin/env bash
set -euo pipefail

# Сетевой bootstrap из исходного vklvikl.py.
# VM_IP и STAND_SUBNET передаются дашбордом для каждой созданной VM.
if [[ -z "\${VM_IP:-}" ]]; then
  echo "VM_IP не задан — настройка статического адреса пропущена"
  exit 0
fi

GUEST_INTERFACE="\${GUEST_INTERFACE:-vmbr0}"
INTERFACES_FILE="\${INTERFACES_FILE:-/etc/network/interfaces}"
PREFIX="\${STAND_SUBNET##*/}"
[[ -n "\${STAND_SUBNET:-}" && "\${PREFIX}" != "\${STAND_SUBNET}" ]] || PREFIX=16
VM_GATEWAY="\${VM_GATEWAY:-10.39.1.1}"

if [[ ! -f "\${INTERFACES_FILE}" ]]; then
  echo "Файл \${INTERFACES_FILE} не найден" >&2
  exit 78
fi
if ! awk -v iface="\${GUEST_INTERFACE}" '
  $1 == "iface" && $2 == iface && $3 == "inet" && $4 == "static" { found=1 }
  END { exit(found ? 0 : 1) }
' "\${INTERFACES_FILE}"; then
  echo "В \${INTERFACES_FILE} нет static-секции iface \${GUEST_INTERFACE}" >&2
  exit 78
fi

sed -i "/^[[:space:]]*iface[[:space:]]\\+\${GUEST_INTERFACE}[[:space:]]\\+inet[[:space:]]\\+static/,/^[^[:space:]]/ s|^\\([ \\t]*address[ \\t]\\+\\).*|\\1\${VM_IP}/\${PREFIX}|" "\${INTERFACES_FILE}"
sed -i "/^[[:space:]]*iface[[:space:]]\\+\${GUEST_INTERFACE}[[:space:]]\\+inet[[:space:]]\\+static/,/^[^[:space:]]/ s|^\\([ \\t]*gateway[ \\t]\\+\\).*|\\1\${VM_GATEWAY}|" "\${INTERFACES_FILE}"
if ! awk -v iface="\${GUEST_INTERFACE}" -v wanted="\${VM_IP}/\${PREFIX}" '
  $1 == "iface" { active=($2 == iface && $3 == "inet" && $4 == "static"); next }
  active && $1 == "address" && $2 == wanted { found=1 }
  END { exit(found ? 0 : 1) }
' "\${INTERFACES_FILE}"; then
  echo "Не удалось записать address \${VM_IP}/\${PREFIX} для \${GUEST_INTERFACE}" >&2
  exit 78
fi

HOSTNAME="$(tr -d '\\n' < /etc/hostname)"
sed -i '/^127\\.0\\.1\\.1/d' /etc/hosts
printf '127.0.1.1 %s\\n' "\${HOSTNAME}" >> /etc/hosts
sed -i "/^[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+.*\${HOSTNAME}/d" /etc/hosts
printf '%s %s.example.local %s\\n' "\${VM_IP}" "\${HOSTNAME}" "\${HOSTNAME}" >> /etc/hosts

network_ready() {
  command -v ip >/dev/null 2>&1 || return 1
  [[ -d "/sys/class/net/\${GUEST_INTERFACE}" ]] || return 1
  FLAGS="$(cat "/sys/class/net/\${GUEST_INTERFACE}/flags" 2>/dev/null)" || return 1
  (( (FLAGS & 1) == 1 )) || return 1
  ip -o -4 addr show dev "\${GUEST_INTERFACE}" scope global 2>/dev/null |
    awk -v wanted="\${VM_IP}/\${PREFIX}" '
      { count++ }
      $4 == wanted { found=1 }
      END { exit(found && count == 1 ? 0 : 1) }
    ' || return 1
  if [[ -d "/sys/class/net/\${GUEST_INTERFACE}/bridge" ]]; then
    for PORT in "/sys/class/net/\${GUEST_INTERFACE}/brif/"*; do
      [[ -e "\${PORT}" ]] || continue
      PORT_NAME="\${PORT##*/}"
      PORT_FLAGS="$(cat "/sys/class/net/\${PORT_NAME}/flags" 2>/dev/null || echo 0)"
      CARRIER="$(cat "/sys/class/net/\${PORT_NAME}/carrier" 2>/dev/null || echo 0)"
      OPERSTATE="$(cat "/sys/class/net/\${PORT_NAME}/operstate" 2>/dev/null || true)"
      if (( (PORT_FLAGS & 1) == 1 )) && [[ "\${CARRIER}" == "1" || "\${OPERSTATE}" == "up" ]]; then
        return 0
      fi
    done
    return 1
  fi
  return 0
}

if command -v ifreload >/dev/null 2>&1; then
  if ! SYNTAX_OUTPUT="$(ifreload -a -s 2>&1)"; then
    echo "Ошибка синтаксиса сетевой конфигурации: \${SYNTAX_OUTPUT}" >&2
    exit 78
  fi
  if ! ifreload -a; then
    echo "ifreload не смог сразу применить конфигурацию; backend выполнит восстановление" >&2
  fi
else
  echo "ifreload не найден; backend безопасно перезагрузит только эту VM" >&2
fi
ip link set dev "\${GUEST_INTERFACE}" up 2>/dev/null || true

if ! command -v ifreload >/dev/null 2>&1 && ! network_ready; then
  exit 75
fi

for _ in $(seq 1 30); do
  if network_ready; then
    echo "Сетевой адрес \${VM_IP}/\${PREFIX} работает на \${GUEST_INTERFACE}"
    exit 0
  fi
  sleep 1
done

ip -details link show dev "\${GUEST_INTERFACE}" >&2 2>/dev/null || true
ip -o -4 addr show dev "\${GUEST_INTERFACE}" >&2 2>/dev/null || true
echo "Сеть ещё не готова; передаём восстановление backend" >&2
exit 75`;
  const routeTitles = {
    overview: "Обзор",
    stands: "Стенды",
    blueprints: "Сценарии",
    checks: "Автопроверки",
    ipam: "IPAM",
    infrastructure: "Инфраструктура",
  };
  const statusLabels = {
    running: "Работает", stopped: "Остановлен", provisioning: "Развёртывается", resetting: "Восстанавливается",
    error: "Ошибка", passed: "Пройдено", warning: "Есть замечания", failed: "Не пройдено",
    idle: "Ожидание", active: "Активна", ended: "Завершена", draft: "Черновик",
    archived: "В архиве", online: "Онлайн", checking: "Проверяется", info: "Информация",
  };
  const state = {
    data: null,
    route: getRoute(),
    standFilter: "all",
    standSearch: "",
    selectedBlueprintId: null,
    editorDirty: false,
    ipam: null,
    ipamLoading: false,
    webActivity: null,
    webActivityLoading: false,
    bulkRollbackPending: new Map(),
    loading: false,
    lastUpdated: null,
  };
  let activeModalToken = 0;
  let modalReturnFocus = null;
  let pendingAdminTokenFinish = null;
  let standDetailPollTimer = null;
  let activeStandDetailId = null;
  let operationPollTimer = null;
  let operationPollBusy = false;
  let webActivityPollTimer = null;
  const standCredentialCache = new Map();

  function getRoute() {
    const value = window.location.hash.replace(/^#\/?/, "").split("?")[0];
    return routes.includes(value) ? value : "overview";
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function safeAccessUrl(value) {
    if (!value) return "";
    try {
      const url = new URL(String(value), window.location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch {
      return "";
    }
  }

  function parseIpv4Cidr(value) {
    const match = String(value || "").trim().match(/^((?:\d{1,3}\.){3}\d{1,3})\/(\d|[12]\d|3[0-2])$/);
    if (!match) return null;
    const octets = match[1].split(".").map(Number);
    if (octets.some(part => part < 0 || part > 255)) return null;
    const addressNumber = octets.reduce((total, part) => total * 256 + part, 0);
    const prefixLength = Number(match[2]);
    const blockSize = 2 ** (32 - prefixLength);
    const networkNumber = Math.floor(addressNumber / blockSize) * blockSize;
    return {
      address: match[1], addressNumber, prefixLength, blockSize, networkNumber,
      broadcastNumber: networkNumber + blockSize - 1,
      cidr: `${match[1]}/${prefixLength}`,
    };
  }

  function numberToIpv4(value) {
    const safe = Math.max(0, Math.min(4294967295, Number(value) || 0));
    return [16777216, 65536, 256, 1].map(divisor => Math.floor(safe / divisor) % 256).join(".");
  }

  function ipamPreview(cidr, vmCount) {
    const parsed = parseIpv4Cidr(cidr);
    if (!parsed) return null;
    const wanted = Math.max(1, Number(vmCount) || 1);
    const addresses = [];
    let cursor = parsed.addressNumber;
    if (parsed.prefixLength <= 30 && (cursor === parsed.networkNumber || cursor === parsed.broadcastNumber)) {
      cursor = parsed.networkNumber + 1;
    }
    while (cursor <= parsed.broadcastNumber && addresses.length < wanted) {
      const boundary = parsed.prefixLength <= 30 && (cursor === parsed.networkNumber || cursor === parsed.broadcastNumber);
      if (!boundary) addresses.push(numberToIpv4(cursor));
      cursor += 1;
    }
    return { ...parsed, first: addresses[0] || "—", last: addresses.at(-1) || "—", available: addresses.length };
  }

  function webUsername(credential, vm = {}) {
    const username = credential?.access_username || credential?.web_username || vm.access_username || credential?.username || "root@pam";
    return username === "root" ? "root@pam" : username;
  }

  function icon(name, className = "") {
    return `<svg class="${className}" aria-hidden="true"><use href="#icon-${name}"></use></svg>`;
  }

  function clamp(value, min = 0, max = 100) {
    return Math.max(min, Math.min(max, Number(value) || 0));
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const token = localStorage.getItem("demoops.adminToken");
    if (token) headers["X-Admin-Token"] = token;
    const request = { method: options.method || "GET", headers };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, request);
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      if (response.status === 401 && options.promptAdmin !== false && !options._retried) {
        const supplied = await requestAdminToken();
        if (supplied) return api(path, { ...options, _retried: true });
      }
      const error = new Error(payload.error || `Ошибка HTTP ${response.status}`);
      error.status = response.status;
      error.code = payload.code;
      throw error;
    }
    return payload;
  }

  function formatNumber(value, digits = 0) {
    return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(Number(value) || 0);
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function relativeTime(value) {
    const date = parseDate(value);
    if (!date) return "—";
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const formatter = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
    if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
    const minutes = Math.round(seconds / 60);
    if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
    return formatter.format(Math.round(hours / 24), "day");
  }

  function dateTime(value) {
    const date = parseDate(value);
    if (!date) return "—";
    return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function duration(start, end = null) {
    const started = parseDate(start);
    const finished = parseDate(end) || new Date();
    if (!started) return "—";
    const totalMinutes = Math.max(0, Math.round((finished - started) / 60000));
    if (totalMinutes < 60) return `${totalMinutes} мин`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return `${hours} ч ${minutes ? `${minutes} мин` : ""}`.trim();
  }

  function statusChip(status, label = null) {
    const cssStatus = ["provisioning", "resetting"].includes(status) ? "deploying" : status === "running-check" ? "checking" : status;
    return `<span class="status status--${escapeHtml(cssStatus)}">${escapeHtml(label || statusLabels[status] || status)}</span>`;
  }

  function resourceBar(label, value, color = "") {
    const safe = clamp(value);
    return `<div class="resource-bar">
      <div class="resource-bar__label"><span>${escapeHtml(label)}</span><strong>${formatNumber(safe, 1)}%</strong></div>
      <div class="progress"><div class="progress__bar ${color ? `progress__bar--${color}` : ""}" style="width:${safe}%"></div></div>
    </div>`;
  }

  function pageHeader(title, subtitle, actions = "") {
    return `<div class="page-header">
      <div class="page-header__copy"><h2 class="page-title">${escapeHtml(title)}</h2><p class="page-subtitle">${escapeHtml(subtitle)}</p></div>
      ${actions ? `<div class="page-actions">${actions}</div>` : ""}
    </div>`;
  }

  function metricCard(label, value, meta, iconName, tone = "") {
    return `<article class="metric-card">
      <div class="metric-card__top"><span class="metric-card__label">${escapeHtml(label)}</span><span class="metric-card__icon ${tone ? `metric-card__icon--${tone}` : ""}">${icon(iconName)}</span></div>
      <div class="metric-card__value">${value}</div>
      <div class="metric-card__meta">${meta}</div>
    </article>`;
  }

  function gauge(value, label, color = "#ed6c23") {
    const safe = clamp(value);
    const radius = 59;
    const circumference = 2 * Math.PI * radius;
    const dash = circumference * safe / 100;
    return `<div class="gauge">
      <svg viewBox="0 0 150 150" role="img" aria-label="${escapeHtml(label)}: ${safe}%">
        <circle cx="75" cy="75" r="${radius}" fill="none" stroke="#edf0f3" stroke-width="11"/>
        <circle cx="75" cy="75" r="${radius}" fill="none" stroke="${color}" stroke-width="11" stroke-linecap="round"
          stroke-dasharray="${dash} ${circumference - dash}" transform="rotate(-90 75 75)"/>
      </svg>
      <div class="gauge__value"><strong>${formatNumber(safe, 1)}%</strong><span>${escapeHtml(label)}</span></div>
    </div>`;
  }

  function lineChart(history, height = 240) {
    if (!history?.length) {
      return `<div class="chart-empty">История появится после первого цикла сбора метрик</div>`;
    }
    const width = 760;
    const top = 18, bottom = 34, left = 36, right = 14;
    const innerW = width - left - right;
    const innerH = height - top - bottom;
    const maxValue = Math.max(100, ...history.flatMap(point => [Number(point.total), Number(point.exam)]));
    const point = (item, index, key) => {
      const x = left + (index / Math.max(history.length - 1, 1)) * innerW;
      const y = top + innerH - (Number(item[key]) / maxValue) * innerH;
      return [x, y];
    };
    const total = history.map((item, index) => point(item, index, "total"));
    const exam = history.map((item, index) => point(item, index, "exam"));
    const toPath = points => points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const areaPath = `${toPath(exam)} L${exam.at(-1)[0].toFixed(1)},${(top + innerH).toFixed(1)} L${exam[0][0].toFixed(1)},${(top + innerH).toFixed(1)} Z`;
    const ticks = [0, 25, 50, 75, 100];
    const grid = ticks.map(value => {
      const y = top + innerH - value / maxValue * innerH;
      return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" stroke="#e8ebee" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
    }).join("");
    const yLabels = ticks.map(value => {
      const y = top + innerH - value / maxValue * innerH;
      return `<span class="chart__axis-label chart__axis-label--y" style="top:${(y / height * 100).toFixed(3)}%">${value}%</span>`;
    }).join("");
    const maxXLabels = window.innerWidth < 700 ? 3 : 6;
    const labelStep = Math.max(1, Math.ceil((history.length - 1) / Math.max(maxXLabels - 1, 1)));
    const labelIndexes = [];
    for (let index = 0; index < history.length; index += labelStep) labelIndexes.push(index);
    if (labelIndexes.at(-1) !== history.length - 1) labelIndexes.push(history.length - 1);
    const xLabels = labelIndexes.map((index, position) => {
      const x = point(history[index], index, "total")[0];
      const edgeClass = position === 0 ? " is-first" : position === labelIndexes.length - 1 ? " is-last" : "";
      return `<span class="chart__axis-label chart__axis-label--x${edgeClass}" style="left:${(x / width * 100).toFixed(3)}%">${escapeHtml(history[index].time)}</span>`;
    }).join("");
    return `<div class="chart" style="height:${height}px" role="img" aria-label="График нагрузки кластера"><svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <defs><linearGradient id="examArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ed6c23" stop-opacity=".20"/><stop offset="1" stop-color="#ed6c23" stop-opacity="0"/></linearGradient></defs>
      ${grid}<path d="${areaPath}" fill="url(#examArea)"/><path d="${toPath(total)}" fill="none" stroke="#9099a6" stroke-width="2" vector-effect="non-scaling-stroke"/><path d="${toPath(exam)}" fill="none" stroke="#ed6c23" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
    </svg>${yLabels}${xLabels}</div>`;
  }

  function emptyState(iconName, title, text, action = "") {
    return `<div class="empty-state"><div class="empty-state__inner"><div class="empty-state__icon">${icon(iconName)}</div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(text)}</p>${action}</div></div>`;
  }

  function renderOverview() {
    const { overview, stands, metrics, activity, integration } = state.data;
    const active = stands.filter(stand => ["running", "provisioning", "resetting"].includes(stand.status));
    const cluster = metrics.cluster;
    const modeLabel = integration.mode === "live" ? "Данные Proxmox в реальном времени" : "Безопасный демонстрационный контур";
    const historyTitle = integration.mode === "live" ? "Последние измерения нагрузки" : "Нагрузка кластера за 24 часа";
    const historySubtitle = integration.mode === "live" ? "История текущего процесса: весь кластер и вклад стендов" : "Весь кластер и вклад стендов демоэкзамена";
    app.innerHTML = `<section class="page">
      ${pageHeader("Операционный обзор", "Состояние экзаменационных стендов и кластера на одном экране.", `<button class="button button--primary" data-open-deploy>${icon("plus")}Развернуть стенд</button>`)}
      <div class="mode-banner mode-banner--${integration.mode}">
        <div><span class="mode-banner__dot"></span><strong>${escapeHtml(integration.cluster)}</strong><span>${escapeHtml(modeLabel)}</span></div>
        <small>Обновлено ${relativeTime(metrics.updated_at)}</small>
      </div>
      <div class="metric-grid">
        ${metricCard("Активные стенды", `${overview.active_stands}<small> / ${overview.total_stands}</small>`, `<span class="metric-card__trend">●</span> ${active.some(s => ["provisioning", "resetting"].includes(s.status)) ? "идёт фоновая операция" : "все операции штатно"}`, "server")}
        ${metricCard("Виртуальные машины", overview.total_vms, "учитываются в стендах", "server", "blue")}
        ${metricCard("Средний результат", `${overview.average_score}%`, `По ${stands.filter(stand => stand.check_score != null).length} последним результатам`, "check", "green")}
        ${metricCard("Нагрузка стендов", `${formatNumber(cluster.exam_cpu, 1)}%`, `${formatNumber(cluster.cpu, 1)}% CPU всего кластера`, "activity", "purple")}
      </div>

      <div class="content-grid content-grid--main">
        <article class="card">
          <div class="card__header"><div class="card__heading"><h3 class="card__title">${historyTitle}</h3><p class="card__subtitle">${historySubtitle}</p></div>
            <div class="chart-legend"><span class="chart-legend__item"><i class="chart-legend__dot chart-legend__dot--muted"></i>Кластер</span><span class="chart-legend__item"><i class="chart-legend__dot"></i>Стенды</span></div></div>
          <div class="card__body">${lineChart(metrics.history, 238)}</div>
          <div class="card__footer"><span class="microcopy">${escapeHtml(metrics.attribution_method)}</span><button class="button button--ghost button--small" data-navigate="infrastructure">Подробнее ${icon("chevron")}</button></div>
        </article>
        <article class="card">
          <div class="card__header"><div class="card__heading"><h3 class="card__title">Состояние нод</h3><p class="card__subtitle">${cluster.online_nodes} из ${cluster.total_nodes} доступны</p></div>${statusChip(cluster.online_nodes === cluster.total_nodes ? "online" : "warning", cluster.online_nodes === cluster.total_nodes ? "Штатно" : "Внимание")}</div>
          <div class="node-list compact-node-list">${metrics.nodes.map(node => `<div class="node-row">
            <div class="node-name"><span class="node-name__icon">${icon("server")}</span><span><strong>${escapeHtml(node.name)}</strong><small>${node.running_vms} VM · ${node.exam_vms} экзамен.</small></span></div>
            ${resourceBar("CPU", node.cpu)}${resourceBar("RAM", node.ram, "blue")}
          </div>`).join("")}</div>
          <div class="card__footer"><span class="microcopy">Резерв CPU: ${formatNumber(100 - cluster.cpu)}%</span><button class="button button--ghost button--small" data-navigate="infrastructure">Все ноды ${icon("chevron")}</button></div>
        </article>
      </div>

      <div class="content-grid content-grid--main">
        <article class="card">
          <div class="card__header"><div class="card__heading"><h3 class="card__title">Активные стенды</h3><p class="card__subtitle">Текущая занятость и результаты проверок</p></div><button class="button button--ghost button--small" data-navigate="stands">Показать все ${icon("chevron")}</button></div>
          <div class="overview-stands">${active.length ? active.slice(0, 4).map(stand => overviewStand(stand)).join("") : emptyState("server", "Нет активных стендов", "Разверните первый стенд из готового сценария.")}</div>
        </article>
        <article class="card">
          <div class="card__header"><div class="card__heading"><h3 class="card__title">Последние события</h3><p class="card__subtitle">Действия операторов и фоновых задач</p></div></div>
          <div class="card__body activity-list">${activity.slice(0, 6).map(activityItem).join("")}</div>
        </article>
      </div>
    </section>`;
  }

  function overviewStand(stand) {
    return `<button class="overview-stand" data-stand-detail="${stand.id}" type="button">
      <span class="overview-stand__state overview-stand__state--${escapeHtml(stand.status)}">${icon("server")}</span>
      <span class="overview-stand__identity"><strong>${escapeHtml(stand.name)}</strong><small>${escapeHtml(stand.blueprint_code || "Без сценария")} · ${escapeHtml(stand.pool_id)}</small></span>
      <span class="overview-stand__users">${icon("server")}<strong>${stand.actual_vm_count || 0}</strong> VM</span>
      <span class="overview-stand__check">${stand.check_score == null ? "—" : `${stand.check_score}%`}<small>проверка</small></span>
      <span>${statusChip(stand.status)}</span>${icon("chevron", "overview-stand__arrow")}
    </button>`;
  }

  function activityItem(item) {
    const icons = { deploy: "server", import: "plus", check: "check", password: "lock", snapshot: "copy", rollback: "refresh", power: "power", script: "code", delete: "trash", edit: "edit" };
    return `<div class="activity-item"><span class="activity-item__icon activity-item__icon--${escapeHtml(item.status)}">${icon(icons[item.kind] || "info")}</span>
      <div class="activity-item__copy"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.detail)}</p></div><time class="activity-item__time" title="${escapeHtml(dateTime(item.created_at))}">${relativeTime(item.created_at)}</time></div>`;
  }

  function renderStands() {
    let stands = state.data.stands;
    if (state.standFilter !== "all") {
      if (state.standFilter === "attention") stands = stands.filter(stand => stand.status === "error" || ["warning", "failed"].includes(stand.check_status));
      else if (state.standFilter === "provisioning") stands = stands.filter(stand => ["provisioning", "resetting"].includes(stand.status));
      else stands = stands.filter(stand => stand.status === state.standFilter);
    }
    if (state.standSearch) {
      const needle = state.standSearch.toLocaleLowerCase("ru");
      stands = stands.filter(stand => [stand.name, stand.pool_id, stand.owner, stand.blueprint_name, stand.node].some(value => String(value || "").toLocaleLowerCase("ru").includes(needle)));
    }
    const counts = {
      all: state.data.stands.length,
      running: state.data.stands.filter(item => item.status === "running").length,
      provisioning: state.data.stands.filter(item => ["provisioning", "resetting"].includes(item.status)).length,
      stopped: state.data.stands.filter(item => item.status === "stopped").length,
      attention: state.data.stands.filter(item => item.status === "error" || ["warning", "failed"].includes(item.check_status)).length,
    };
    app.innerHTML = `<section class="page">
      ${pageHeader("Стенды", "Управляйте пулами, виртуальными машинами, доступами и автопроверками.", `<button class="button button--danger" data-rollback-all-stands ${state.data.stands.length ? "" : "disabled"}>${icon("refresh")}Вернуть все стенды к start</button><button class="button" data-import-pool>${icon("plus")}Добавить существующий pool</button><button class="button button--primary" data-open-deploy>${icon("plus")}Развернуть стенд</button>`)}
      <div class="toolbar"><div class="toolbar__primary"><label class="search-field">${icon("search")}<input id="stand-search" type="search" value="${escapeHtml(state.standSearch)}" placeholder="Название, pool ID, владелец…"></label>
        <div class="filter-tabs" role="tablist">${[["all", "Все"], ["running", "Работают"], ["provisioning", "В процессе"], ["stopped", "Остановлены"], ["attention", "Требуют внимания"]].map(([key, label]) => `<button class="filter-tab ${state.standFilter === key ? "is-active" : ""}" data-stand-filter="${key}" type="button">${label}<span>${counts[key]}</span></button>`).join("")}</div></div>
        <div class="toolbar-actions"><button class="button" data-refresh>${icon("refresh")}Обновить</button></div></div>
      ${stands.length ? `<div class="table-card"><div class="table-wrap"><table class="data-table stands-table"><thead><tr><th>Стенд</th><th>Состояние</th><th>Ресурсы</th><th>Автопроверка</th><th></th></tr></thead><tbody>
        ${stands.map(standRow).join("")}</tbody></table></div></div>` : `<div class="card">${emptyState("server", "Стенды не найдены", state.standSearch ? "Измените запрос или сбросьте фильтры." : "Разверните стенд из готового сценария.", `<button class="button button--primary" data-open-deploy>${icon("plus")}Развернуть</button>`)}</div>`}
    </section>`;
    const search = document.querySelector("#stand-search");
    search?.addEventListener("input", event => {
      state.standSearch = event.target.value;
      window.clearTimeout(search._timer);
      search._timer = window.setTimeout(() => {
        renderStands();
        const restored = document.querySelector("#stand-search");
        restored?.focus();
        restored?.setSelectionRange(restored.value.length, restored.value.length);
      }, 180);
    });
  }

  function standRow(stand) {
    return `<tr class="clickable-row" data-stand-detail="${stand.id}">
      <td><div class="entity-cell"><span class="entity-icon">${icon("server")}</span><span><strong class="cell-title">${escapeHtml(stand.name)}</strong><small class="cell-subtitle mono">${escapeHtml(stand.pool_id)} · ${escapeHtml(stand.node || "авто")}${stand.origin === "imported" ? " · подключён" : ""}</small></span></div></td>
      <td>${statusChip(stand.status)}${["provisioning", "resetting"].includes(stand.status) ? `<div class="inline-progress"><div class="progress"><div class="progress__bar progress__bar--blue" style="width:${clamp(stand.progress)}%"></div></div><small>${stand.progress}%</small></div>` : ""}</td>
      <td><div class="resource-pair">${resourceBar("CPU", stand.cpu)}${resourceBar("RAM", stand.ram, "blue")}</div></td>
      <td>${stand.check_status === "running" ? statusChip("checking", "Выполняется") : stand.check_score == null && stand.check_status === "failed" ? statusChip("failed", "Ошибка запуска") : stand.check_score == null ? `<span class="muted">Не запускалась</span>` : `<div class="score-cell"><strong class="score score--${stand.check_score >= 90 ? "good" : stand.check_score >= 70 ? "warn" : "bad"}">${stand.check_score}%</strong><small>${relativeTime(stand.last_check)}</small></div>`}</td>
      <td class="cell-actions"><button class="button button--small table-check-button" data-stand-action="run_check" data-stand-id="${stand.id}" type="button" ${stand.status !== "running" ? "disabled" : ""}>${icon("check")}Автопроверка</button><button class="icon-button" data-stand-detail="${stand.id}" title="Открыть">${icon("chevron")}</button></td>
    </tr>`;
  }

  function renderBlueprints() {
    const blueprints = state.data.blueprints;
    app.innerHTML = `<section class="page">
      ${pageHeader("Сценарии развёртывания", "Сценарий связывает шаблон Proxmox, bootstrap и правила проверки стенда. Сеть и количество VM задаются при развёртывании.", `<button class="button button--primary" data-blueprint-new>${icon("plus")}Новый сценарий</button>`)}
      <div class="blueprint-summary"><div>${icon("code")}<span><strong>${blueprints.filter(item => item.status === "active").length} опубликовано</strong><small>${blueprints.filter(item => item.status === "draft").length} черновик · ${blueprints.length} шаблонов в каталоге</small></span></div>
        <div class="blueprint-summary__note">${icon("shield")} Скрипты выполняются только внутри гостевых VM через Guest Agent</div></div>
      <div class="blueprint-grid">${blueprints.map(blueprintCard).join("")}
        <button class="blueprint-card blueprint-card--new" data-blueprint-new type="button"><span>${icon("plus")}</span><strong>Создать сценарий</strong><small>Настройте новый экзаменационный модуль</small></button>
      </div>
    </section>`;
  }

  function blueprintCard(blueprint) {
    const tones = { "Сети": "orange", "Системы": "blue", "Безопасность": "purple", "Базы данных": "green" };
    return `<article class="blueprint-card blueprint-card--${tones[blueprint.category] || "slate"}">
      <div class="blueprint-card__top"><span class="blueprint-card__mark">${icon(blueprint.category === "Безопасность" ? "shield" : blueprint.category === "Сети" ? "activity" : "server")}</span><span>${statusChip(blueprint.status)}</span></div>
      <div class="blueprint-card__body"><div class="blueprint-code">${escapeHtml(blueprint.code)} · v${escapeHtml(blueprint.version)}</div><h3>${escapeHtml(blueprint.name)}</h3><p>${escapeHtml(blueprint.description)}</p>
        <div class="tag-list">${(blueprint.tags || []).map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div></div>
      <div class="blueprint-specs"><span><small>Шаблон VMID</small><strong class="mono">${blueprint.template_vmid}</strong></span><span><small>Версия</small><strong>${escapeHtml(blueprint.version)}</strong></span><span><small>Проверки</small><strong>${blueprint.checks_count || "—"}</strong></span></div>
      <div class="blueprint-card__footer"><button class="button button--primary button--small" data-open-deploy="${blueprint.id}" ${blueprint.status !== "active" ? "disabled title=\"Сначала опубликуйте сценарий\"" : ""}>${icon("power")}Развернуть</button>
        <div><button class="icon-button" data-blueprint-duplicate="${blueprint.id}" title="Дублировать">${icon("copy")}</button><button class="icon-button" data-blueprint-edit="${blueprint.id}" title="Редактировать">${icon("edit")}</button><button class="icon-button" data-blueprint-delete="${blueprint.id}" title="Удалить">${icon("trash")}</button></div></div>
    </article>`;
  }

  function renderChecks() {
    const blueprints = state.data.blueprints;
    if (!state.selectedBlueprintId || !blueprints.some(item => item.id === state.selectedBlueprintId)) state.selectedBlueprintId = blueprints[0]?.id || null;
    const selected = blueprints.find(item => item.id === state.selectedBlueprintId);
    const runs = state.data.checks.filter(run => !selected || run.blueprint_id === selected.id);
    if (!selected) {
      app.innerHTML = `<section class="page">${pageHeader("Автопроверки", "Редактируйте и запускайте проверочные сценарии.")}<div class="card">${emptyState("check", "Нет сценариев", "Сначала создайте сценарий развёртывания.")}</div></section>`;
      return;
    }
    app.innerHTML = `<section class="page">
      ${pageHeader("Автопроверки", "Редактируйте проверочный код прямо в дашборде и запускайте его на выбранном стенде.", `<button class="button" data-run-check>${icon("power")}Запустить на стенде</button><button class="button button--primary" data-save-check>${icon("check")}Сохранить</button>`)}
      <div class="check-workspace">
        <aside class="check-sidebar card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Сценарии</h3><p class="card__subtitle">${blueprints.length} в каталоге</p></div></div>
          <div class="check-script-list">${blueprints.map(item => `<button class="check-script-item ${item.id === selected.id ? "is-active" : ""}" data-select-check="${item.id}" type="button"><span class="check-script-item__icon">${icon("terminal")}</span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)} · ${item.checks_count || "—"} проверки</small></span>${item.status === "active" ? `<i class="online-dot"></i>` : ""}</button>`).join("")}</div>
          <div class="check-sidebar__footer"><span>${icon("info")} Выполнение изолировано внутри VM</span></div>
        </aside>
        <div class="check-main">
          <div class="editor-shell"><div class="editor-toolbar"><div class="editor-file">${icon("terminal")}<span>${escapeHtml(selected.code.toLocaleLowerCase())}-autocheck.${selected.autocheck_script.trimStart().startsWith("# PowerShell") ? "ps1" : "sh"}</span><i id="editor-dirty-indicator" class="editor-saved">Сохранено</i></div>
            <div class="editor-actions"><button class="icon-button" data-copy-editor title="Копировать">${icon("copy")}</button><button class="icon-button" data-expand-editor title="Развернуть">${icon("external")}</button></div></div>
            <textarea class="code-editor" id="autocheck-editor" spellcheck="false" aria-label="Код автопроверки">${escapeHtml(selected.autocheck_script)}</textarea>
            <div class="editor-statusbar"><span>UTF-8</span><span>${selected.autocheck_script.split("\n").length} строк</span><span>${selected.autocheck_script.trimStart().startsWith("# PowerShell") ? "PowerShell" : "Shell"}</span><span>Таймаут 120 c</span></div>
          </div>
          <div class="alert alert--info">${icon("info")}<div><strong>Контракт результата</strong><br>Возвращайте JSON с полями <code>name</code> и <code>ok</code> либо строки формата <code>название:ok</code>. Сервер дашборда этот код не исполняет.</div></div>
        </div>
      </div>
      <article class="card"><div class="card__header"><div class="card__heading"><h3 class="card__title">История запусков</h3><p class="card__subtitle">${escapeHtml(selected.name)}</p></div><button class="button button--small" data-run-check>${icon("power")}Новый запуск</button></div>
        ${runs.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Стенд</th><th>Результат</th><th>Проверки</th><th>Время</th><th>Длительность</th><th></th></tr></thead><tbody>${runs.slice(0, 8).map(run => `<tr><td><strong class="cell-title">${escapeHtml(run.stand_name)}</strong><small class="cell-subtitle">${escapeHtml(run.blueprint_code || "")}${run.vmid ? ` · VM ${Number(run.vmid)}` : " · весь стенд"}</small></td><td>${run.status === "running" ? statusChip("checking", "Выполняется") : statusChip(run.status)}</td><td><strong>${run.score == null ? "—" : `${run.score}%`}</strong><small class="cell-subtitle">${run.total ? `${run.passed} из ${run.total}` : "Ожидание результатов"}</small></td><td>${dateTime(run.started_at)}</td><td>${run.duration_ms ? `${formatNumber(run.duration_ms / 1000, 1)} c` : "—"}</td><td class="cell-actions"><button class="icon-button" data-check-detail="${run.id}" title="Результаты">${icon("chevron")}</button></td></tr>`).join("")}</tbody></table></div>` : emptyState("check", "Запусков пока нет", "Запустите автопроверку на совместимом стенде.")}</article>
    </section>`;
    const editor = document.querySelector("#autocheck-editor");
    editor?.addEventListener("input", () => {
      state.editorDirty = true;
      const indicator = document.querySelector("#editor-dirty-indicator");
      if (indicator) { indicator.textContent = "Есть изменения"; indicator.className = "editor-unsaved"; }
    });
    editor?.addEventListener("keydown", event => {
      if (event.key === "Tab") {
        event.preventDefault();
        const start = editor.selectionStart, end = editor.selectionEnd;
        editor.value = `${editor.value.slice(0, start)}  ${editor.value.slice(end)}`;
        editor.selectionStart = editor.selectionEnd = start + 2;
        editor.dispatchEvent(new Event("input"));
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "s") {
        event.preventDefault(); saveAutocheck();
      }
    });
  }

  function renderInfrastructure() {
    const { metrics, stands, integration } = state.data;
    const cluster = metrics.cluster;
    const topStands = [...stands].filter(stand => stand.status === "running").sort((a, b) => b.cpu - a.cpu);
    const historySubtitle = integration.mode === "live" ? "Измерения текущего процесса; оранжевым — управляемые VM" : "Почасовые значения; оранжевым — VM из управляемых пулов стендов";
    const clusterHealthy = integration.connected && cluster.online_nodes === cluster.total_nodes && cluster.cpu < 85 && cluster.ram < 90;
    app.innerHTML = `<section class="page">
      ${pageHeader("Инфраструктура", "Фактическая загрузка Proxmox и оценка вклада экзаменационных VM в ресурсы кластера.", `<button class="button" data-refresh>${icon("refresh")}Обновить метрики</button>`)}
      <div class="infra-hero card"><div class="infra-hero__copy"><span class="infra-kicker">${escapeHtml(integration.cluster)} · ${integration.mode === "live" ? "LIVE" : "DEMO"}</span><h3>${clusterHealthy ? "Кластер работает стабильно" : "Инфраструктура требует внимания"}</h3><p>${cluster.online_nodes} из ${cluster.total_nodes} нод онлайн. Экзаменационные стенды используют <strong>${formatNumber(cluster.exam_cpu, 1)}%</strong> доступной CPU-ёмкости и <strong>${formatNumber(cluster.exam_ram, 1)}%</strong> памяти.</p>
        <div class="infra-tags"><span>${icon("server")} ${metrics.nodes.reduce((sum, node) => sum + node.running_vms, 0)} VM работают</span><span>${icon("activity")} ${100 - Math.round(cluster.cpu)}% CPU в резерве</span><span>${icon("shield")} TLS ${integration.mode === "live" ? "проверяется" : "не требуется"}</span></div></div>
        <div class="infra-hero__share">${gauge(cluster.exam_cpu, "вклад стендов", "#ed6c23")}</div></div>
      <div class="gauge-grid card"><div>${gauge(cluster.cpu, "CPU кластера", "#ed6c23")}</div><div>${gauge(cluster.ram, "RAM кластера", "#397fbc")}</div><div>${gauge(cluster.disk, "Хранилище", "#7b61a8")}</div><div>${gauge(cluster.exam_ram, "RAM стендов", "#2a9d69")}</div></div>
      <article class="card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Динамика использования CPU</h3><p class="card__subtitle">${historySubtitle}</p></div><div class="chart-legend"><span class="chart-legend__item"><i class="chart-legend__dot chart-legend__dot--muted"></i>Кластер</span><span class="chart-legend__item"><i class="chart-legend__dot"></i>Демоэкзамен</span></div></div><div class="card__body">${lineChart(metrics.history, 270)}</div></article>
      <div class="content-grid content-grid--infra">
        <article class="card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Ноды Proxmox</h3><p class="card__subtitle">Распределение нагрузки и экзаменационных VM</p></div></div><div class="infra-node-list">${metrics.nodes.map(infraNode).join("")}</div></article>
        <article class="card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Вклад по стендам</h3><p class="card__subtitle">Текущая оценка CPU работающих стендов</p></div></div><div class="stand-impact-list">${topStands.map((stand, index) => `<button type="button" data-stand-detail="${stand.id}" class="impact-row"><span class="impact-rank">${index + 1}</span><span class="impact-copy"><strong>${escapeHtml(stand.name)}</strong><small>${stand.vm_count} VM · ${escapeHtml(stand.node)}</small><i><b style="width:${clamp(stand.cpu)}%"></b></i></span><span class="impact-value">${formatNumber(stand.cpu, 1)}%</span></button>`).join("") || emptyState("activity", "Нет нагрузки", "Все стенды остановлены.")}</div></article>
      </div>
      ${renderWebActivityCard(state.webActivity)}
      <div class="attribution-note">${icon("info")}<div><strong>Что означает «нагрузка стендов»</strong><p>${escapeHtml(metrics.attribution_method)}. Это практическая оценка по VM; служебные процессы гипервизора, общий page cache и нагрузку shared storage нельзя строго причинно отнести к одному стенду.</p></div></div>
    </section>`;
    if (!state.webActivity) window.setTimeout(() => loadWebActivity({ background: true }), 0);
    else scheduleWebActivityRefresh();
  }

  function infraNode(node) {
    return `<div class="infra-node"><div class="infra-node__head"><div class="node-name"><span class="node-name__icon">${icon("server")}</span><span><strong>${escapeHtml(node.name)}</strong><small>uptime ${node.uptime_days} дн. · ${node.running_vms} VM</small></span></div>${statusChip(node.status)}</div>
      <div class="infra-node__metrics"><div><span>CPU</span><strong>${formatNumber(node.cpu, 1)}%</strong><small>стенды ${formatNumber(node.exam_cpu, 1)}%</small></div><div><span>RAM</span><strong>${formatNumber(node.ram, 1)}%</strong><small>стенды ${formatNumber(node.exam_ram, 1)}%</small></div><div><span>Диск</span><strong>${formatNumber(node.disk, 1)}%</strong><small>${node.exam_vms} exam VM</small></div></div>
      <div class="stacked-load" title="Оранжевый: экзаменационные стенды"><i style="width:${clamp(node.cpu)}%"><b style="width:${clamp(node.cpu ? node.exam_cpu / node.cpu * 100 : 0)}%"></b></i></div></div>`;
  }

  function renderWebActivityCard(payload) {
    if (!payload) {
      return `<article class="card" id="web-activity-card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Веб-активность на стендах</h3><p class="card__subtitle">Проверяем pveproxy через QEMU Guest Agent</p></div></div><div class="detail-loading"><span class="spinner"></span><p>Ищем недавние обращения web UI…</p></div></article>`;
    }
    if (payload.error) {
      return `<article class="card" id="web-activity-card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Веб-активность на стендах</h3><p class="card__subtitle">Не удалось получить данные</p></div><button class="button button--small" data-refresh-web-activity>${icon("refresh")}Повторить</button></div><div class="alert alert--error">${icon("alert")}<div>${escapeHtml(payload.error)}</div></div></article>`;
    }
    const activity = Array.isArray(payload.activity) ? payload.activity : [];
    const errors = Array.isArray(payload.errors) ? payload.errors : [];
    const requestedVms = Number(payload.requested_vms || 0);
    const scannedVms = Number(payload.scanned_vms || 0);
    const completelyUnavailable = requestedVms > 0 && scannedVms === 0;
    const activeCount = activity.filter(item => item.state === "active").length;
    const rows = activity.map(item => `<tr><td><strong class="cell-title">${escapeHtml(item.stand_name || `Стенд #${item.stand_id || "—"}`)}</strong><small class="cell-subtitle">${escapeHtml(item.vm_name || `VM ${item.vmid}`)} · VMID ${Number(item.vmid)}</small></td><td><strong class="mono">${escapeHtml(item.source_ip)}</strong><small class="cell-subtitle">IP клиента</small></td><td><strong class="mono">${escapeHtml(item.user)}</strong><small class="cell-subtitle">учётная запись Proxmox</small></td><td>${item.state === "active" ? statusChip("running", "Активен") : statusChip("idle", "Недавно активен")}<small class="cell-subtitle">${relativeTime(item.last_seen)}</small></td><td><strong>${formatNumber(item.request_count)}</strong><small class="cell-subtitle">запросов за окно</small></td></tr>`).join("");
    const subtitle = completelyUnavailable
      ? `Проверка недоступна для ${requestedVms} VM`
      : `${activeCount} активных сейчас · ${activity.length} замечено за ${Math.round(Number(payload.window_seconds || 180) / 60)} мин.`;
    const resultBody = rows
      ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Стенд / VM</th><th>Клиент</th><th>Логин</th><th>Активность</th><th>Запросы</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : completelyUnavailable
        ? `<div class="alert alert--warning">${icon("alert")}<div><strong>Не удалось проверить ни одну VM.</strong><br>Проверьте QEMU Guest Agent, право VM.GuestAgent.Unrestricted и доступ к /var/log/pveproxy/access.log.</div></div>`
        : emptyState("users", "Открытых web UI не обнаружено", "За последние три минуты не было регулярных запросов интерфейса Proxmox.");
    return `<article class="card" id="web-activity-card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Веб-активность на стендах</h3><p class="card__subtitle">${subtitle}</p></div><button class="button button--small" data-refresh-web-activity>${icon("refresh")}Обновить</button></div><div class="alert alert--info">${icon("info")}<div><strong>Это недавняя активность, а не точный список сессий.</strong><br>${escapeHtml(payload.notice || "Proxmox не хранит серверный список открытых браузерных вкладок.")}</div></div>${resultBody}${errors.length && !completelyUnavailable ? `<div class="card__footer"><span class="microcopy">Не удалось проверить ${errors.length} из ${requestedVms} VM. Нужен работающий QEMU Guest Agent и доступ к pveproxy log.</span></div>` : ""}</article>`;
  }

  function scheduleWebActivityRefresh() {
    if (webActivityPollTimer) window.clearTimeout(webActivityPollTimer);
    webActivityPollTimer = null;
    if (state.route === "infrastructure" && !document.hidden) {
      webActivityPollTimer = window.setTimeout(() => loadWebActivity({ background: true }), 30000);
    }
  }

  async function loadWebActivity({ force = false, background = false } = {}) {
    if (state.webActivityLoading) return;
    if (state.webActivity && !force && !background) { scheduleWebActivityRefresh(); return; }
    state.webActivityLoading = true;
    try {
      state.webActivity = await api(`/api/web-activity${force ? "?force=1" : ""}`, { promptAdmin: !background });
    } catch (error) {
      state.webActivity = { activity: [], errors: [], error: error.message };
    } finally {
      state.webActivityLoading = false;
      const card = document.querySelector("#web-activity-card");
      if (card && state.route === "infrastructure") card.outerHTML = renderWebActivityCard(state.webActivity);
      scheduleWebActivityRefresh();
    }
  }

  async function loadIpam({ force = false } = {}) {
    if (state.ipamLoading) return;
    if (state.ipam && !force) return;
    state.ipamLoading = true;
    try {
      state.ipam = await api("/api/ipam");
    } catch (error) {
      state.ipam = { reservations: [], summary: {}, error: error.message };
    } finally {
      state.ipamLoading = false;
      if (state.route === "ipam") renderIpam();
    }
  }

  function renderIpam() {
    const payload = state.ipam;
    if (!payload) {
      app.innerHTML = `<section class="page">${pageHeader("IPAM", "Резервирование IPv4-адресов для экзаменационных стендов.", `<button class="button" data-refresh-ipam>${icon("refresh")}Обновить IPAM</button>`)}`
        + `<div class="card"><div class="detail-loading"><span class="spinner"></span><p>Загружаем адресный план…</p></div></div></section>`;
      window.setTimeout(() => loadIpam(), 0);
      return;
    }
    const reservations = Array.isArray(payload.reservations) ? payload.reservations : [];
    const pools = Array.isArray(payload.pools) ? payload.pools : [];
    const summary = payload.summary || {};
    const assigned = Number(summary.assigned ?? reservations.filter(item => item.status === "assigned").length);
    const reserved = Number(summary.reserved ?? reservations.filter(item => item.status === "reserved").length);
    const stands = Number(summary.stands ?? new Set(reservations.map(item => item.stand_id).filter(Boolean)).size);
    const ranges = Number(summary.pools ?? (pools.length || new Set(reservations.map(item => item.requested_cidr).filter(Boolean)).size));
    const rows = reservations.map(item => `<tr>
      <td><strong class="mono ipam-address">${escapeHtml(item.address)}</strong><small class="cell-subtitle mono">/${Number(item.prefix_length ?? (String(item.requested_cidr || "").split("/")[1] || 0))}</small></td>
      <td><strong class="cell-title">${escapeHtml(item.vm_name || `VMID ${item.vmid || "—"}`)}</strong><small class="cell-subtitle mono">VMID ${escapeHtml(item.vmid || "—")} · #${Number(item.vm_index ?? 0)}</small></td>
      <td><strong class="cell-title">${escapeHtml(item.stand_name || `Стенд #${item.stand_id || "—"}`)}</strong><small class="cell-subtitle mono">${escapeHtml(item.requested_cidr || "—")}</small></td>
      <td>${statusChip(item.status || "reserved", item.status === "assigned" ? "Назначен" : item.status === "reserved" ? "Зарезервирован" : item.status)}</td>
      <td><span class="muted">${relativeTime(item.updated_at || item.created_at)}</span></td>
    </tr>`).join("");
    app.innerHTML = `<section class="page">
      ${pageHeader("IPAM", "IP-адреса выдаются с введённой оператором точки старта; занятые адреса пропускаются автоматически.", `<button class="button" data-refresh-ipam>${icon("refresh")}Обновить IPAM</button><button class="button button--primary" data-open-deploy>${icon("plus")}Зарезервировать через развёртывание</button>`)}
      ${payload.error ? `<div class="alert alert--error">${icon("alert")}<div><strong>Не удалось получить IPAM</strong><br>${escapeHtml(payload.error)}</div></div>` : ""}
      <div class="metric-grid ipam-metrics">
        ${metricCard("Адресов учтено", formatNumber(summary.total ?? reservations.length), "в активных резервациях", "activity")}
        ${metricCard("Назначено VM", formatNumber(assigned), "адрес передан гостевой машине", "server", "green")}
        ${metricCard("Ожидают назначения", formatNumber(reserved), "зарезервированы на время деплоя", "clock", "blue")}
        ${metricCard("Стенды и диапазоны", `${formatNumber(stands)}<small> / ${formatNumber(ranges)}</small>`, "стендов / стартовых CIDR", "shield", "purple")}
      </div>
      <div class="ipam-explainer">${icon("info")}<div><strong>Стартовый адрес не приводится к началу сети</strong><p>Запись <span class="mono">10.39.4.0/16</span> означает выдачу с <span class="mono">10.39.4.0</span>, затем <span class="mono">10.39.4.1</span> и далее. Настоящий адрес сети, broadcast и уже занятые IP будут пропущены.</p></div></div>
      ${pools.length ? `<div class="ipam-pool-grid">${pools.map(pool => `<article class="ipam-pool-card"><div><span>Стартовый запрос</span><strong class="mono">${escapeHtml(pool.requested_cidr)}</strong></div><p class="mono">${escapeHtml(pool.first_address || "—")} → ${escapeHtml(pool.last_address || "—")}</p><footer><span>${formatNumber(pool.assigned)} назначено</span><span>${formatNumber(pool.reservations)} всего</span><span>${formatNumber(pool.stands)} стенд.</span></footer></article>`).join("")}</div>` : ""}
      <div class="table-card"><div class="card__header"><div class="card__heading"><h3 class="card__title">Резервации адресов</h3><p class="card__subtitle">${reservations.length} записей в текущем адресном плане</p></div></div>${reservations.length ? `<div class="table-wrap"><table class="data-table ipam-table"><thead><tr><th>IPv4</th><th>Виртуальная машина</th><th>Стенд / запрос</th><th>Состояние</th><th>Обновлено</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyState("activity", "Резерваций пока нет", "Адреса появятся после запуска развёртывания со стартовым IPv4 и префиксом.")}</div>
    </section>`;
  }

  function render() {
    if (!state.data) return;
    document.querySelector("#page-title").textContent = routeTitles[state.route];
    document.querySelectorAll(".nav-link[data-route]").forEach(link => {
      const active = link.dataset.route === state.route;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    const renderers = { overview: renderOverview, stands: renderStands, blueprints: renderBlueprints, checks: renderChecks, ipam: renderIpam, infrastructure: renderInfrastructure };
    renderers[state.route]();
    updateShell();
  }

  function updateShell() {
    if (!state.data) return;
    const count = state.data.stands.filter(stand => ["running", "resetting"].includes(stand.status)).length;
    const countNode = document.querySelector("#nav-stands-count");
    countNode.textContent = count;
    countNode.hidden = count === 0;
    const integration = state.data.integration;
    const clusterState = document.querySelector("#cluster-state");
    clusterState.classList.toggle("is-error", !integration.connected);
    clusterState.querySelector("strong").textContent = integration.connected ? `${integration.cluster} подключён` : "Proxmox недоступен";
    clusterState.querySelector("small").textContent = integration.mode === "demo" ? "Демонстрационный режим" : integration.message || integration.host;
    const sync = document.querySelector("#sync-state .sync-state__copy");
    if (sync) sync.textContent = state.lastUpdated ? `Обновлено ${relativeTime(state.lastUpdated)}` : "Данные актуальны";
    document.querySelector(".notification-dot").hidden = state.data.overview.attention === 0;
  }

  async function loadData({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    const refreshButtons = document.querySelectorAll("#global-refresh, [data-refresh]");
    refreshButtons.forEach(button => button.classList.add("is-spinning"));
    try {
      const data = await api("/api/bootstrap");
      state.data = data;
      state.lastUpdated = new Date();
      syncOperationPolling();
      if (silent) updateShell(); else render();
    } catch (error) {
      if (!state.data) {
        app.innerHTML = `<section class="page"><div class="load-error">${icon("alert")}<h2>Не удалось загрузить дашборд</h2><p>${escapeHtml(error.message)}</p><button class="button button--primary" data-refresh>${icon("refresh")}Повторить</button></div></section>`;
      } else if (!silent) toast(error.message, "error");
    } finally {
      state.loading = false;
      refreshButtons.forEach(button => button.classList.remove("is-spinning"));
    }
  }

  function toast(message, type = "success", timeout = 4200) {
    const item = document.createElement("div");
    item.className = `toast toast--${type}`;
    item.innerHTML = `<span class="toast__icon">${icon(type === "error" ? "alert" : type === "warning" ? "info" : "check")}</span><div class="toast__copy"><strong>${type === "error" ? "Ошибка" : type === "warning" ? "Обратите внимание" : "Готово"}</strong><p>${escapeHtml(message)}</p></div><button class="toast__close" type="button" aria-label="Закрыть">${icon("x")}</button>`;
    item.querySelector("button").addEventListener("click", () => item.remove());
    toastRoot.append(item);
    window.setTimeout(() => { item.classList.add("is-leaving"); window.setTimeout(() => item.remove(), 250); }, timeout);
  }

  function showModal({ title, subtitle = "", body, footer = "", size = "", className = "" }) {
    if (activeStandDetailId !== null && !className.includes("stand-detail-modal")) stopStandDetailPolling();
    activeModalToken += 1;
    if (!modalRoot.innerHTML) modalReturnFocus = document.activeElement;
    modalRoot.innerHTML = `<div class="modal-backdrop"><section class="modal ${size ? `modal--${size}` : ""} ${className}" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <header class="modal__header"><div><h2 class="modal__title" id="modal-title">${escapeHtml(title)}</h2>${subtitle ? `<p class="modal__subtitle">${escapeHtml(subtitle)}</p>` : ""}</div><button class="icon-button modal__close" type="button" data-close-modal aria-label="Закрыть">${icon("x")}</button></header>
      <div class="modal__body">${body}</div>${footer ? `<footer class="modal__footer">${footer}</footer>` : ""}</section></div>`;
    document.body.classList.add("is-modal-open");
    window.setTimeout(() => modalRoot.querySelector(".modal__body input:not([type=hidden]), .modal__body textarea, .modal__body select, .modal__body button, .modal__close")?.focus(), 10);
  }

  function closeModal() {
    if (pendingAdminTokenFinish) {
      const finish = pendingAdminTokenFinish;
      finish(null);
      return;
    }
    stopStandDetailPolling();
    activeModalToken += 1;
    modalRoot.innerHTML = "";
    document.body.classList.remove("is-modal-open");
    if (modalReturnFocus?.isConnected) modalReturnFocus.focus();
    modalReturnFocus = null;
  }

  function requestAdminToken(manual = false) {
    return new Promise(resolve => {
      const configured = Boolean(localStorage.getItem("demoops.adminToken"));
      const body = `<form id="admin-token-form"><div class="credential-intro"><span>${icon("shield")}</span><div><h3>Доступ к управляющим операциям</h3><p>Токен сравнивается сервером с DASHBOARD_ADMIN_TOKEN и хранится только в localStorage этого браузера.</p></div></div><label class="field"><span class="field-label">Административный токен</span><input class="input mono" id="admin-token-input" type="password" minlength="12" autocomplete="current-password" placeholder="${configured ? "Введите новый токен для замены" : "Не менее 12 символов"}" required></label><div class="alert alert--info">${icon("info")} Для сетевого доступа всё равно используйте HTTPS и аутентификацию reverse proxy.</div></form>`;
      const footer = `${configured ? `<button class="button button--danger" id="remove-admin-token" type="button">Удалить локальный токен</button>` : ""}<button class="button" data-token-cancel type="button">Отмена</button><button class="button button--primary" type="submit" form="admin-token-form">${icon("lock")}Сохранить</button>`;
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        pendingAdminTokenFinish = null;
        closeModal();
        resolve(value);
      };
      pendingAdminTokenFinish = finish;
      showModal({ title: manual ? "Настройка доступа" : "Требуется административный токен", subtitle: "Защищённый режим Deployer", body, footer });
      modalRoot.querySelector("#admin-token-form").addEventListener("submit", event => {
        event.preventDefault();
        const token = modalRoot.querySelector("#admin-token-input").value.trim();
        if (token.length < 12) return;
        localStorage.setItem("demoops.adminToken", token);
        finish(token);
        if (manual) toast("Административный токен сохранён");
      });
      modalRoot.querySelector("[data-token-cancel]").addEventListener("click", () => finish(null));
      modalRoot.querySelector(".modal__close").addEventListener("click", () => finish(null));
      modalRoot.querySelector("#remove-admin-token")?.addEventListener("click", () => {
        localStorage.removeItem("demoops.adminToken");
        finish(null);
        toast("Локальный административный токен удалён", "warning");
      });
    });
  }

  function openImportPoolModal() {
    const pools = (state.data.pools || []).filter(pool => !pool.imported);
    const blueprints = state.data.blueprints.filter(item => item.status !== "archived");
    if (!pools.length) { toast("Нет свободных Proxmox pools для добавления", "warning"); return; }
    if (!blueprints.length) { toast("Сначала создайте сценарий для привязки pool", "warning"); return; }
    const poolOptions = pools.map(pool => `<option value="${escapeHtml(pool.pool_id)}">${escapeHtml(pool.pool_id)}${pool.vm_count == null ? "" : ` · ${pool.vm_count} VM`}${pool.comment ? ` · ${escapeHtml(pool.comment)}` : ""}</option>`).join("");
    const blueprintOptions = blueprints.map(item => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("");
    const body = `<form id="pool-import-form"><div class="alert alert--info">${icon("info")} Pool и его VM не создаются заново. Дашборд только подключит их к мониторингу и выбранному сценарию.</div><div class="form-grid"><label class="field field--full"><span class="field-label">Существующий Proxmox pool</span><select class="input mono" name="pool_id" id="import-pool-select" required>${poolOptions}</select></label><label class="field"><span class="field-label">Название стенда</span><input class="input" name="name" id="import-pool-name" value="${escapeHtml(pools[0].pool_id)}" required></label><label class="field"><span class="field-label">Сценарий и автопроверка</span><select class="input" name="blueprint_id" required>${blueprintOptions}</select></label><label class="field field--full"><span class="field-label">Ответственный</span><input class="input" name="owner" value="Администратор"></label></div><div class="alert alert--warning">${icon("shield")} При удалении подключённого стенда из дашборда исходный pool и его VM останутся в Proxmox.</div></form>`;
    showModal({ title: "Добавить существующий pool", subtitle: "Подключение ресурсов без клонирования", body, footer: `<button class="button" data-close-modal type="button">Отмена</button><button class="button button--primary" type="submit" form="pool-import-form">${icon("plus")}Добавить pool</button>`, size: "wide" });
    const form = modalRoot.querySelector("#pool-import-form");
    const select = modalRoot.querySelector("#import-pool-select");
    const nameInput = modalRoot.querySelector("#import-pool-name");
    let nameTouched = false;
    nameInput.addEventListener("input", () => { nameTouched = true; });
    select.addEventListener("change", () => { if (!nameTouched) nameInput.value = select.value; });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const submit = modalRoot.querySelector('button[type="submit"]');
      submit.disabled = true;
      const values = Object.fromEntries(new FormData(form).entries());
      values.blueprint_id = Number(values.blueprint_id);
      try {
        const stand = await api("/api/pools/import", { method: "POST", body: values });
        closeModal(); toast(`Pool ${stand.pool_id} добавлен`); await loadData();
      } catch (error) { submit.disabled = false; toast(error.message, "error"); }
    });
  }

  function renderDeployIpamPreview(preview, vmCount) {
    if (!preview) {
      return `${icon("info")}<div><strong>Укажите стартовый адрес с префиксом</strong><small>Например: 10.39.4.0/16. Без значения VM продолжат использовать сеть шаблона.</small></div>`;
    }
    if (!preview.available) {
      return `${icon("alert")}<div><strong>В указанном диапазоне нет доступных адресов</strong><small>Выберите другой стартовый IPv4 или более широкий префикс.</small></div>`;
    }
    const incomplete = preview.available < Number(vmCount);
    return `${icon(incomplete ? "alert" : "shield")}<div><strong>${escapeHtml(preview.first)} → ${escapeHtml(preview.last)}</strong><small>${incomplete ? `Вместилось только ${preview.available} из ${vmCount} VM.` : `${vmCount} адресов будут зарезервированы по порядку.`} Занятые адреса, адрес сети и broadcast IPAM пропустит автоматически.</small></div>`;
  }

  function openDeployWizard(preselectedId = null) {
    if (!state.data) { toast("Данные ещё загружаются. Повторите через несколько секунд", "warning"); return; }
    const available = state.data.blueprints.filter(item => item.status === "active");
    const existingPools = (state.data.pools || []).filter(pool => !pool.imported);
    if (!available.length) { toast("Нет опубликованных сценариев для развёртывания", "warning"); return; }
    const defaultPoolId = `exam-${new Date().toISOString().slice(5, 10).replace("-", "")}-${String(Date.now()).slice(-3)}`;
    const model = {
      step: 1, blueprint_id: Number(preselectedId) || available[0].id,
      name: "", pool_id: defaultPoolId, new_pool_id: defaultPoolId,
      use_existing_pool: false, existing_pool_id: existingPools[0]?.pool_id || "",
      node: "auto", vm_count: 1, subnet: "", start_ip: "", bridge: "",
      owner: "Администратор",
    };
    const draw = () => {
      const blueprint = available.find(item => item.id === Number(model.blueprint_id)) || available[0];
      const steps = ["Сценарий", "VM и сеть", "Доступ", "Проверка"];
      let body = `<div class="wizard-steps">${steps.map((label, index) => `<div class="wizard-step ${model.step === index + 1 ? "is-active" : model.step > index + 1 ? "is-done" : ""}"><span>${model.step > index + 1 ? icon("check") : index + 1}</span><small>${label}</small></div>`).join("")}</div>`;
      if (model.step === 1) {
        body += `<div class="wizard-section"><h3>Выберите сценарий</h3><p>Шаблон и проверочный код будут закреплены за новым стендом.</p><div class="scenario-picker">${available.map(item => `<button type="button" class="scenario-option ${item.id === Number(model.blueprint_id) ? "is-selected" : ""}" data-wizard-blueprint="${item.id}"><span>${icon(item.category === "Безопасность" ? "shield" : "server")}</span><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)} · шаблон VMID ${item.template_vmid}</small></div><i>${icon("check")}</i></button>`).join("")}</div>
          <label class="option-row"><span>${icon("server")}</span><span><strong>Добавить в существующий pool</strong><small>${existingPools.length ? "Новые VM будут добавлены в выбранный Proxmox pool; его текущие VM и сам pool останутся нетронутыми." : "Свободных существующих pools сейчас нет."}</small></span><input type="checkbox" id="deploy-use-existing-pool" ${model.use_existing_pool ? "checked" : ""} ${existingPools.length ? "" : "disabled"}></label>
          <div class="form-grid"><label class="field"><span class="field-label">Название стенда</span><input class="input" id="deploy-name" value="${escapeHtml(model.name)}" placeholder="Например, ДЭ-24 · Группа 4" required></label><label class="field" id="deploy-new-pool-field" ${model.use_existing_pool ? "hidden" : ""}><span class="field-label">Название нового pool</span><input class="input mono" id="deploy-pool" value="${escapeHtml(model.new_pool_id)}" pattern="[A-Za-z0-9_.-]+" ${model.use_existing_pool ? "" : "required"}><small class="field-hint">Латиница, цифры, точка, дефис</small></label><label class="field" id="deploy-existing-pool-field" ${model.use_existing_pool ? "" : "hidden"}><span class="field-label">Существующий Proxmox pool</span><select class="input mono" id="deploy-existing-pool" ${model.use_existing_pool ? "required" : ""}>${existingPools.map(pool => `<option value="${escapeHtml(pool.pool_id)}" ${pool.pool_id === model.existing_pool_id ? "selected" : ""}>${escapeHtml(pool.pool_id)}${pool.vm_count == null ? "" : ` · ${pool.vm_count} VM`}</option>`).join("")}</select><small class="field-hint">Pool не будет удалён вместе со стендом</small></label></div></div>`;
      } else if (model.step === 2) {
        const preview = ipamPreview(model.subnet, model.vm_count);
        body += `<div class="wizard-section"><h3>Параметры развёртывания и IPAM</h3><p>IPAM резервирует последовательные свободные адреса, начиная именно с указанного IPv4. Все VM создаются как linked clone.</p><div class="deploy-plan-card"><div class="deploy-plan-card__icon">${icon("server")}</div><div><strong>${escapeHtml(blueprint.name)}</strong><small>Шаблон VMID ${blueprint.template_vmid} · связанные клоны</small></div><span>Linked clone</span></div>
          <div class="form-grid"><label class="field"><span class="field-label">Количество VM</span><input class="input" id="deploy-vm-count" type="number" min="1" max="50" value="${model.vm_count}"></label><label class="field"><span class="field-label">Стартовый IPv4 / префикс</span><input class="input mono" id="deploy-subnet" value="${escapeHtml(model.subnet)}" placeholder="10.39.4.0/16"><small class="field-hint">Например, 10.39.4.0/16 начнёт выдачу с 10.39.4.0, а не с 10.39.0.1</small></label><label class="field field--full"><span class="field-label">Bridge Proxmox</span><input class="input mono" id="deploy-bridge" value="${escapeHtml(model.bridge)}" placeholder="Оставьте пустым — сеть сохранится из шаблона"><small class="field-hint">Указывайте bridge только если он существует на каждой выбранной ноде</small></label><label class="field field--full"><span class="field-label">Политика размещения</span><select class="input" id="deploy-node"><option value="auto" ${model.node === "auto" ? "selected" : ""}>Автоматически: storage + нагрузка</option>${state.data.metrics.nodes.filter(node => node.status === "online").map(node => `<option value="${escapeHtml(node.name)}" ${model.node === node.name ? "selected" : ""}>${escapeHtml(node.name)} · CPU ${formatNumber(node.cpu)}% · RAM ${formatNumber(node.ram)}%</option>`).join("")}</select><small class="field-hint">Linked clone попадёт на другую ноду только если она видит диски шаблона; иначе он останется на ноде шаблона.</small></label></div>
          <div class="ipam-preview" id="deploy-ipam-preview">${renderDeployIpamPreview(preview, model.vm_count)}</div>
          <div class="capacity-preview"><div><span>Количество</span><strong>${model.vm_count} VM</strong></div><div><span>Старт IPAM</span><strong class="mono">${escapeHtml(preview?.first || "DHCP")}</strong></div><div><span>Bridge</span><strong class="mono">${escapeHtml(model.bridge || "из шаблона")}</strong></div><div><span>Тип</span><strong>Linked clone</strong></div></div></div>`;
      } else if (model.step === 3) {
        body += `<div class="wizard-section"><h3>Доступ и автоматические действия</h3><p>Стенд создаётся бессрочно и будет работать, пока оператор не остановит или не удалит его.</p><div class="form-grid"><label class="field field--full"><span class="field-label">Ответственный</span><input class="input" id="deploy-owner" value="${escapeHtml(model.owner)}"></label></div>
          <div class="option-list"><label class="option-row"><span>${icon("lock")}</span><span><strong>Учётные данные для каждой VM</strong><small>Логин и пароль будут доступны в карточке стенда после готовности машины</small></span><input type="checkbox" checked disabled></label><label class="option-row"><span>${icon("copy")}</span><span><strong>Начальный снимок «start»</strong><small>Контрольная точка исходного состояния создаётся автоматически для каждой VM</small></span><input type="checkbox" checked disabled></label><label class="option-row"><span>${icon("check")}</span><span><strong>Автопроверка после деплоя</strong><small>Запускается вручную после готовности стенда</small></span><input type="checkbox" disabled></label></div></div>`;
      } else {
        const preview = ipamPreview(model.subnet, model.vm_count);
        body += `<div class="wizard-section"><div class="confirm-hero"><span>${icon("check")}</span><h3>План готов к запуску</h3><p>Проверьте параметры. Развёртывание продолжится в фоне.</p></div><dl class="review-list"><div><dt>Стенд</dt><dd><strong>${escapeHtml(model.name)}</strong><small class="mono">${escapeHtml(model.pool_id)}</small></dd></div><div><dt>Сценарий</dt><dd><strong>${escapeHtml(blueprint.name)}</strong><small>${escapeHtml(blueprint.code)} · VMID ${blueprint.template_vmid}</small></dd></div><div><dt>Топология</dt><dd><strong>${model.vm_count} VM · Linked clone</strong><small>${escapeHtml(model.node === "auto" ? "Автораспределение" : model.node)}</small></dd></div><div><dt>IPAM</dt><dd><strong class="mono">${escapeHtml(model.subnet || "DHCP")}</strong><small class="mono">${preview ? `${escapeHtml(preview.first)} → ${escapeHtml(preview.last)}` : escapeHtml(model.bridge || "bridge из шаблона")}</small></dd></div><div><dt>Ответственный</dt><dd><strong>${escapeHtml(model.owner)}</strong><small>Бессрочный стенд · snapshot start автоматически</small></dd></div></dl>
          <div class="alert alert--warning">${icon("alert")} ${model.use_existing_pool ? `Новые VM будут добавлены в существующий pool ${escapeHtml(model.pool_id)}. Текущие ресурсы pool не изменяются.` : "В live-режиме будут созданы реальные VM и новый пул Proxmox."} Операция появится в журнале аудита.</div></div>`;
      }
      const footer = `<button class="button" type="button" ${model.step === 1 ? "data-close-modal" : "data-wizard-back"}>${model.step === 1 ? "Отмена" : "Назад"}</button><button class="button button--primary" type="button" ${model.step === 4 ? "data-wizard-submit" : "data-wizard-next"}>${model.step === 4 ? `${icon("power")}Начать развёртывание` : `Продолжить ${icon("chevron")}`}</button>`;
      showModal({ title: "Развернуть новый стенд", subtitle: `Шаг ${model.step} из 4 · ${steps[model.step - 1]}`, body, footer, size: "wide", className: "deploy-modal" });
      modalRoot.querySelector("#deploy-use-existing-pool")?.addEventListener("change", event => {
        model.use_existing_pool = event.currentTarget.checked;
        const newPoolField = modalRoot.querySelector("#deploy-new-pool-field");
        const existingPoolField = modalRoot.querySelector("#deploy-existing-pool-field");
        if (newPoolField) newPoolField.hidden = model.use_existing_pool;
        if (existingPoolField) existingPoolField.hidden = !model.use_existing_pool;
      });
      const updateIpamPreview = () => {
        const cidr = modalRoot.querySelector("#deploy-subnet")?.value || "";
        const vmCount = Number(modalRoot.querySelector("#deploy-vm-count")?.value || 1);
        const target = modalRoot.querySelector("#deploy-ipam-preview");
        if (target) target.innerHTML = renderDeployIpamPreview(ipamPreview(cidr, vmCount), vmCount);
      };
      modalRoot.querySelector("#deploy-subnet")?.addEventListener("input", updateIpamPreview);
      modalRoot.querySelector("#deploy-vm-count")?.addEventListener("input", updateIpamPreview);
      modalRoot.querySelectorAll("[data-wizard-blueprint]").forEach(button => button.addEventListener("click", () => { commitDeployStep(model, false); model.blueprint_id = Number(button.dataset.wizardBlueprint); draw(); }));
      modalRoot.querySelector("[data-wizard-back]")?.addEventListener("click", () => { commitDeployStep(model, false); model.step -= 1; draw(); });
      modalRoot.querySelector("[data-wizard-next]")?.addEventListener("click", () => {
        if (!commitDeployStep(model)) return;
        model.step += 1; draw();
      });
      modalRoot.querySelector("[data-wizard-submit]")?.addEventListener("click", async event => {
        const button = event.currentTarget;
        button.disabled = true; button.classList.add("is-loading");
        try {
          model.start_ip = parseIpv4Cidr(model.subnet)?.address || "";
          const stand = await api("/api/stands", { method: "POST", body: model });
          state.ipam = null;
          closeModal(); toast(`Стенд «${stand.name}» поставлен на развёртывание`); state.route = "stands"; window.location.hash = "stands"; await loadData(); await openStandDetail(stand.id);
        } catch (error) { button.disabled = false; button.classList.remove("is-loading"); toast(error.message, "error"); }
      });
    };
    draw();
  }

  function commitDeployStep(model, validate = true) {
    if (model.step === 1) {
      const name = modalRoot.querySelector("#deploy-name");
      const pool = modalRoot.querySelector("#deploy-pool");
      const useExisting = Boolean(modalRoot.querySelector("#deploy-use-existing-pool")?.checked);
      const existingPool = modalRoot.querySelector("#deploy-existing-pool");
      if (!name || !pool) return true;
      if (validate && !name.value.trim()) { name.classList.add("is-invalid"); name.focus(); toast("Укажите название стенда", "warning"); return false; }
      const selectedPoolId = useExisting ? existingPool?.value.trim() || "" : pool.value.trim();
      const invalidTarget = useExisting ? existingPool : pool;
      if (validate && !/^[A-Za-z0-9_.-]+$/.test(selectedPoolId)) { invalidTarget?.classList.add("is-invalid"); invalidTarget?.focus(); toast(useExisting ? "Выберите существующий pool" : "Название pool содержит недопустимые символы", "warning"); return false; }
      model.name = name.value.trim();
      model.use_existing_pool = useExisting;
      model.existing_pool_id = useExisting ? selectedPoolId : model.existing_pool_id;
      model.new_pool_id = useExisting ? model.new_pool_id : selectedPoolId;
      model.pool_id = selectedPoolId;
    } else if (model.step === 2) {
      const node = modalRoot.querySelector("#deploy-node");
      const vmCount = modalRoot.querySelector("#deploy-vm-count"), subnet = modalRoot.querySelector("#deploy-subnet"), bridge = modalRoot.querySelector("#deploy-bridge");
      if (node) model.node = node.value;
      if (vmCount) {
        const parsedVmCount = Number(vmCount.value);
        if (validate && (!Number.isInteger(parsedVmCount) || parsedVmCount < 1 || parsedVmCount > 50)) {
          vmCount.classList.add("is-invalid"); vmCount.focus(); toast("Количество VM должно быть от 1 до 50", "warning"); return false;
        }
        model.vm_count = Number.isFinite(parsedVmCount) ? clamp(parsedVmCount, 1, 50) : 1;
      }
      if (subnet) {
        const value = subnet.value.trim();
        const preview = value ? ipamPreview(value, model.vm_count) : null;
        if (validate && value && !preview) {
          subnet.classList.add("is-invalid"); subnet.focus(); toast("Укажите стартовый IPv4 и префикс в формате 10.39.4.0/16", "warning"); return false;
        }
        if (validate && preview && preview.available < model.vm_count) {
          subnet.classList.add("is-invalid"); subnet.focus(); toast("В диапазоне недостаточно адресов для выбранного количества VM", "warning"); return false;
        }
        model.subnet = value;
        model.start_ip = parseIpv4Cidr(value)?.address || "";
      }
      if (bridge) {
        const value = bridge.value.trim();
        if (validate && value && !/^[A-Za-z0-9_.:-]{1,64}$/.test(value)) {
          bridge.classList.add("is-invalid"); bridge.focus(); toast("Некорректное имя bridge", "warning"); return false;
        }
        model.bridge = value;
      }
    } else if (model.step === 3) {
      const owner = modalRoot.querySelector("#deploy-owner");
      if (owner) model.owner = owner.value.trim() || "Администратор";
    }
    return true;
  }

  function openBlueprintEditor(id = null) {
    const original = id ? state.data.blueprints.find(item => item.id === Number(id)) : null;
    const blueprint = original || {
      code: "NEW-01", name: "", description: "", category: "Общий", version: "1.0", status: "draft",
      template_vmid: 0, tags: [], deploy_script: VKLVIKL_BOOTSTRAP_SCRIPT,
    };
    const templates = state.data.templates || [];
    const knownTemplate = templates.some(item => Number(item.vmid) === Number(blueprint.template_vmid));
    const templateOptions = `<option value="">${templates.length ? "Выберите QEMU-шаблон" : "Нет доступных QEMU-шаблонов"}</option>${!knownTemplate && Number(blueprint.template_vmid) > 0 ? `<option value="${Number(blueprint.template_vmid)}" selected>Текущий VMID ${Number(blueprint.template_vmid)}</option>` : ""}${templates.map(item => `<option value="${item.vmid}" ${Number(item.vmid) === Number(blueprint.template_vmid) ? "selected" : ""}>${escapeHtml(item.name)} · VMID ${item.vmid}${item.node ? ` · ${escapeHtml(item.node)}` : ""}</option>`).join("")}`;
    const body = `<form id="blueprint-form" class="blueprint-form"><div class="form-section"><h3>Основные сведения</h3><div class="form-grid">
      <label class="field"><span class="field-label">Название</span><input class="input" name="name" value="${escapeHtml(blueprint.name)}" required></label><label class="field"><span class="field-label">Код</span><input class="input mono" name="code" value="${escapeHtml(blueprint.code)}" required></label>
      <label class="field field--full"><span class="field-label">Описание</span><textarea class="input textarea" name="description">${escapeHtml(blueprint.description)}</textarea></label>
      <label class="field"><span class="field-label">Категория</span><select class="input" name="category">${["Сети", "Системы", "Безопасность", "Базы данных", "Общий"].map(value => `<option ${blueprint.category === value ? "selected" : ""}>${value}</option>`).join("")}</select></label><label class="field"><span class="field-label">Статус</span><select class="input" name="status"><option value="draft" ${blueprint.status === "draft" ? "selected" : ""}>Черновик</option><option value="active" ${blueprint.status === "active" ? "selected" : ""}>Опубликован</option><option value="archived" ${blueprint.status === "archived" ? "selected" : ""}>Архив</option></select></label>
      <label class="field"><span class="field-label">Версия</span><input class="input" name="version" value="${escapeHtml(blueprint.version)}"></label><label class="field"><span class="field-label">Теги через запятую</span><input class="input" name="tags" value="${escapeHtml((blueprint.tags || []).join(", "))}"></label></div></div>
      <div class="form-section"><h3>Шаблон Proxmox</h3><p>Количество VM, подсеть и bridge оператор укажет при развёртывании стенда. Используются только связанные клоны.</p><div class="form-grid"><label class="field field--full"><span class="field-label">QEMU-шаблон</span><select class="input mono" name="template_vmid" required>${templateOptions}</select><small class="field-hint">Показываются VM, отмеченные в Proxmox как Template</small></label></div></div>
      <div class="form-section"><h3>Bootstrap-скрипт</h3><p>Скрипт запускается через QEMU Guest Agent внутри каждой созданной VM.</p><div class="editor-shell editor-shell--compact"><div class="editor-toolbar"><div class="editor-file">${icon("terminal")}deploy.sh</div><div class="editor-actions"><button class="editor-template-button" type="button" data-use-vklvikl-bootstrap>Вставить из vklvikl</button><span class="editor-safety">guest only</span></div></div><textarea class="code-editor" name="deploy_script" spellcheck="false">${escapeHtml(blueprint.deploy_script)}</textarea></div><small class="field-hint">Исходный скрипт рассчитан на Linux с интерфейсом vmbr0 и gateway 10.39.1.1; значения можно изменить через GUEST_INTERFACE и VM_GATEWAY.</small></div></form>`;
    const footer = `<button class="button" data-close-modal type="button">Отмена</button><button class="button button--primary" type="submit" form="blueprint-form">${icon("check")}${original ? "Сохранить изменения" : "Создать сценарий"}</button>`;
    showModal({ title: original ? "Редактировать сценарий" : "Новый сценарий", subtitle: original ? `${original.code} · версия ${original.version}` : "Новый сценарий создаётся как черновик", body, footer, size: "large" });
    const form = modalRoot.querySelector("#blueprint-form");
    modalRoot.querySelector("[data-use-vklvikl-bootstrap]").addEventListener("click", () => {
      const editor = form.elements.deploy_script;
      const placeholder = "#!/usr/bin/env bash\nset -euo pipefail\n\n# Подготовка гостевой VM";
      const hasCustomCode = editor.value.trim() && editor.value.trim() !== placeholder;
      if (hasCustomCode && editor.value !== VKLVIKL_BOOTSTRAP_SCRIPT && !window.confirm("Заменить текущий bootstrap код скриптом из vklvikl?")) return;
      editor.value = VKLVIKL_BOOTSTRAP_SCRIPT;
      editor.focus();
      toast("Bootstrap из vklvikl вставлен. Сохраните сценарий");
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const submit = modalRoot.querySelector("button[type=submit]"); submit.disabled = true;
      const values = Object.fromEntries(new FormData(form).entries());
      values.tags = values.tags.split(",").map(item => item.trim()).filter(Boolean);
      try {
        await api(original ? `/api/blueprints/${original.id}` : "/api/blueprints", { method: original ? "PATCH" : "POST", body: values });
        closeModal(); toast(original ? "Сценарий сохранён" : "Сценарий создан"); await loadData();
      } catch (error) { submit.disabled = false; toast(error.message, "error"); }
    });
  }

  function normalizeVmCredentials(payload) {
    const raw = Array.isArray(payload) ? payload : payload?.credentials ?? payload?.vms ?? payload?.items ?? [];
    const entries = Array.isArray(raw) ? raw.map(item => [item?.vmid, item]) : Object.entries(raw || {});
    const credentials = new Map();
    entries.forEach(([key, value]) => {
      const item = typeof value === "string" ? { password: value } : (value || {});
      const vmid = Number(item.vmid ?? key);
      if (Number.isInteger(vmid)) credentials.set(vmid, item);
    });
    return credentials;
  }

  function vmHasStartSnapshot(vm) {
    const snapshots = Array.isArray(vm?.snapshots) ? vm.snapshots : [];
    return vm?.last_snapshot === "start"
      || vm?.has_start_snapshot === true
      || vm?.start_snapshot_created === true
      || vm?.start_snapshot === true
      || snapshots.some(item => String(item?.name ?? item) === "start");
  }

  function renderVmRow(stand, vm, credential, credentialState) {
    const numericVmid = Number(vm.vmid);
    const hasVmid = Number.isInteger(numericVmid) && numericVmid > 0;
    const username = webUsername(credential, vm);
    const password = credential?.password || credential?.access_password || "";
    const accessUrl = safeAccessUrl(vm.access_url || vm.web_url);
    const hasStartSnapshot = vmHasStartSnapshot(vm);
    const standBusy = ["provisioning", "resetting"].includes(stand.status) || Boolean(stand.bulk_rollback_pending);
    const canRollbackVm = hasVmid && hasStartSnapshot && Boolean(vm.credential_recoverable)
      && !standBusy && stand.check_status !== "running" && vm.check_status !== "running";
    const checkLabel = vm.check_status === "running" ? "автопроверка идёт" : vm.check_score == null ? "не проверялась" : `автопроверка ${formatNumber(vm.check_score)}%`;
    const passwordUnverified = credentialState === "loaded" && !password
      && Boolean(credential?.credential_recoverable || vm.credential_recoverable);
    const passwordText = password ? "••••••••••••" : credentialState === "locked" ? "Нужен токен" : credentialState === "error" ? "Ошибка загрузки" : passwordUnverified ? "Не подтверждён" : "Не выдан";
    const passwordHint = passwordUnverified
      ? "Повторите возврат к snapshot start: пароль скрыт до подтверждения QEMU Guest Agent."
      : "Смените пароль этой VM или загрузите защищённые доступы.";
    const accessButton = accessUrl && hasVmid && !standBusy
      ? `<button class="button button--primary vm-open-button" type="button" data-open-vm-stand data-url="${escapeHtml(accessUrl)}" data-user="${escapeHtml(username)}" data-password="${escapeHtml(password)}" data-vm-name="${escapeHtml(vm.name)}" data-vmid="${numericVmid}">${icon("external")}Перейти к стенду</button>`
      : `<button class="button button--primary vm-open-button" type="button" disabled title="Web URL ещё не получен">${icon("chevron")}Перейти к стенду</button>`;
    return `<article class="vm-row">
      <span class="vm-state vm-state--${escapeHtml(vm.status)}"></span>
      <div class="vm-main"><div class="vm-main__title"><strong>${escapeHtml(vm.name)}</strong>${statusChip(vm.status)}</div><small class="mono">${hasVmid ? `VMID ${numericVmid}` : "VM создаётся"} · ${escapeHtml(vm.ip || "IP резервируется")}</small><div class="vm-meta"><span>${icon("server")}${escapeHtml(vm.node || stand.node || "авто")}</span><span class="${hasStartSnapshot ? "is-ready" : ""}">${icon("copy")}${hasStartSnapshot ? "snapshot start готов" : "snapshot start"}</span><span class="${vm.check_status === "running" ? "is-checking" : vm.check_score == null ? "" : vm.check_score >= 90 ? "is-ready" : vm.check_score >= 70 ? "is-warning" : "is-failed"}">${icon("check")}${escapeHtml(checkLabel)}</span></div></div>
      <div class="vm-access"><div class="vm-access__hint">${icon("lock")}Доступ к веб-интерфейсу</div><div class="vm-credentials"><div><span>Логин</span><strong class="mono">${escapeHtml(username)}</strong><button type="button" data-copy-vm-login data-value="${escapeHtml(username)}">${icon("copy")}Копировать</button></div><div><span>Пароль</span><strong class="mono" data-vm-secret>${escapeHtml(passwordText)}</strong>${password ? `<div class="vm-secret-actions"><button type="button" data-reveal-vm-password data-password="${escapeHtml(password)}">${icon("eye")}Показать</button><button type="button" data-copy-vm-password data-value="${escapeHtml(password)}">${icon("copy")}Копировать</button></div>` : ""}</div></div>${password ? `<button class="vm-copy-access" type="button" data-copy-vm-credential data-user="${escapeHtml(username)}" data-password="${escapeHtml(password)}">${icon("copy")}Скопировать парой</button>` : `<small class="vm-access__empty">${escapeHtml(passwordHint)}</small>`}</div>
      <div class="vm-actions">${accessButton}<div><button class="button button--small" type="button" data-vm-action="snapshot" data-stand-id="${stand.id}" data-vmid="${hasVmid ? numericVmid : ""}" data-vm-name="${escapeHtml(vm.name)}" ${!hasVmid || standBusy ? "disabled" : ""}>${icon("copy")}Снимок</button><button class="button button--small" type="button" data-vm-action="rollback_start" data-stand-id="${stand.id}" data-vmid="${hasVmid ? numericVmid : ""}" data-vm-name="${escapeHtml(vm.name)}" title="Вернуть эту VM к snapshot start" ${canRollbackVm ? "" : "disabled"}>${icon("refresh")}Вернуть к start</button><button class="button button--small" type="button" data-vm-action="password" data-stand-id="${stand.id}" data-vmid="${hasVmid ? numericVmid : ""}" data-vm-name="${escapeHtml(vm.name)}" ${!hasVmid || vm.status !== "running" || standBusy ? "disabled" : ""}>${icon("lock")}Пароль</button><button class="button button--small" type="button" data-vm-action="run_check" data-stand-id="${stand.id}" data-vmid="${hasVmid ? numericVmid : ""}" data-vm-name="${escapeHtml(vm.name)}" ${!hasVmid || vm.status !== "running" || standBusy ? "disabled" : ""}>${icon("check")}Автопроверка</button></div></div>
    </article>`;
  }

  function renderStandDetailModal(stand, credentials = new Map(), credentialState = "locked", credentialError = "", { updateExisting = false } = {}) {
    const runCount = stand.vms.filter(vm => vm.status === "running").length;
    const standBusy = ["provisioning", "resetting"].includes(stand.status) || Boolean(stand.bulk_rollback_pending);
    const missingStartCount = stand.vms.filter(vm => !vmHasStartSnapshot(vm)).length;
    const missingCredentialCount = stand.vms.filter(vm => !vm.credential_recoverable).length;
    const canRollbackStart = stand.vms.length > 0 && !standBusy && !missingStartCount && !missingCredentialCount
      && stand.check_status !== "running" && !stand.vms.some(vm => vm.check_status === "running");
    const rollbackTitle = missingStartCount
      ? `Snapshot start отсутствует у ${missingStartCount} VM`
      : missingCredentialCount ? `Сохранённый пароль отсутствует у ${missingCredentialCount} VM`
      : standBusy ? "Дождитесь завершения фоновой операции" : "Все VM будут возвращены к snapshot start";
    standCredentialCache.set(Number(stand.id), { stand, credentials });
    const credentialCount = stand.vms.filter(vm => credentials.get(Number(vm.vmid))?.password || credentials.get(Number(vm.vmid))?.access_password).length;
    const credentialControl = credentialState === "loaded"
      ? `<span class="credential-state credential-state--ready">${icon("check")}Доступы загружены: ${credentialCount}</span><button class="button button--small" type="button" data-copy-stand-access="${stand.id}" ${credentialCount ? "" : "disabled"}>${icon("copy")}Все IP, логины и пароли</button>`
      : `<button class="button button--small" type="button" data-load-stand-credentials="${stand.id}">${icon("lock")}Показать логины и пароли</button><button class="button button--small" type="button" data-copy-stand-access="${stand.id}" ${stand.vms.some(vm => Number(vm.vmid) > 0) ? "" : "disabled"}>${icon("copy")}Скопировать все доступы</button>`;
    const credentialNotice = credentialState === "locked"
      ? `<div class="alert alert--info">${icon("info")}<div><strong>Пароли защищены административным токеном.</strong><br>Нажмите «Показать логины и пароли», чтобы загрузить их для этого стенда.</div></div>`
      : credentialState === "error" ? `<div class="alert alert--warning">${icon("alert")}<div><strong>Не удалось получить сохранённые доступы.</strong><br>${escapeHtml(credentialError || "Можно сменить пароль отдельно у нужной VM.")}</div></div>` : "";
    const body = `<div class="stand-detail">
      <div class="stand-detail__hero"><div class="stand-detail__icon">${icon("server")}</div><div><div class="stand-detail__title"><h3>${escapeHtml(stand.name)}</h3>${statusChip(stand.status)}</div><p class="mono">${escapeHtml(stand.pool_id)} · ${escapeHtml(stand.blueprint_name || "Без сценария")} · ${escapeHtml(stand.node)}</p></div><div class="stand-detail__score"><strong>${stand.check_score == null ? "—" : `${stand.check_score}%`}</strong><small>автопроверка</small></div></div>
      ${standBusy ? `<div class="deploy-detail"><div><strong>${stand.status === "resetting" ? "Возврат к исходному состоянию" : "Развёртывание выполняется"}</strong><span>${stand.progress}%</span></div><div class="progress"><div class="progress__bar progress__bar--blue" style="width:${stand.progress}%"></div></div><small>${stand.status === "resetting" ? "VM откатываются к snapshot start и запускаются заново." : "Мастер продолжает работу в фоне."} Окно можно закрыть крестиком.</small></div>` : ""}
      ${stand.status === "error" && stand.last_error ? `<div class="alert alert--error">${icon("alert")}<div><strong>Причина ошибки операции</strong><br><span class="mono">${escapeHtml(stand.last_error)}</span></div></div>` : ""}
      <div class="detail-stat-grid"><div><span>Машины</span><strong>${runCount} / ${stand.vm_count}</strong><small>запущено</small></div><div><span>CPU</span><strong>${formatNumber(stand.cpu, 1)}%</strong><small>текущая оценка</small></div><div><span>RAM</span><strong>${formatNumber(stand.ram, 1)}%</strong><small>на ноде</small></div><div><span>Режим работы</span><strong>Бессрочно</strong><small>автоотключение выключено</small></div></div>
      <div class="detail-actions"><button class="button" data-stand-action="${stand.status === "stopped" ? "start" : "stop"}" data-stand-id="${stand.id}" ${!["running", "stopped"].includes(stand.status) || !stand.vms.length ? "disabled" : ""}>${icon("power")}${stand.status === "stopped" ? "Запустить" : "Остановить"}</button><button class="button" data-stand-action="restart" data-stand-id="${stand.id}" ${stand.status !== "running" ? "disabled" : ""}>${icon("refresh")}Перезапустить</button><button class="button" data-stand-action="snapshot" data-stand-id="${stand.id}" ${!stand.vms.length || standBusy ? "disabled" : ""}>${icon("copy")}Снимок всех VM</button><button class="button" data-stand-action="rollback_start" data-stand-id="${stand.id}" title="${escapeHtml(rollbackTitle)}" ${canRollbackStart ? "" : "disabled"}>${icon("refresh")}Вернуть к изначальному состоянию</button><button class="button" data-stand-action="password" data-stand-id="${stand.id}" ${stand.status !== "running" ? "disabled" : ""}>${icon("lock")}Пароль всех VM</button><button class="button" data-edit-stand="${stand.id}" ${standBusy ? "disabled" : ""}>${icon("edit")}Параметры</button><button class="button button--dark" data-stand-action="run_check" data-stand-id="${stand.id}" ${stand.status !== "running" ? "disabled" : ""}>${icon("check")}Автопроверка</button></div>
      ${credentialNotice}
      <div class="detail-columns detail-columns--single"><section><div class="detail-section-title"><div><h4>Виртуальные машины</h4><small>Доступы можно копировать по одному или одной кнопкой в формате IP | логин | пароль</small></div><div class="detail-section-tools"><span class="detail-count">${stand.vms.length}</span>${credentialControl}</div></div><div class="vm-list">${stand.vms.map(vm => renderVmRow(stand, vm, credentials.get(Number(vm.vmid)), credentialState)).join("") || `<p class="muted-block">Машины ещё не созданы</p>`}</div></section></div>
      <div class="detail-meta"><span>${icon("activity")} Создан ${dateTime(stand.created_at)}</span><span>${icon("lock")} Пароль менялся ${relativeTime(stand.password_updated_at)}</span><span>${icon("users")} Ответственный: ${escapeHtml(stand.owner)}</span></div>
    </div>`;
    const importedPool = stand.origin === "imported";
    const footer = `<button class="button button--danger" data-delete-stand="${stand.id}" type="button" ${standBusy ? "disabled" : ""}>${icon("trash")}${importedPool ? "Убрать pool из списка" : "Удалить стенд"}</button><button class="button" data-close-modal type="button">Закрыть</button>`;
    const existing = updateExisting ? modalRoot.querySelector(`.modal[data-stand-detail-id="${Number(stand.id)}"]`) : null;
    if (existing) {
      const bodyNode = existing.querySelector(".modal__body");
      const footerNode = existing.querySelector(".modal__footer");
      const scrollTop = bodyNode?.scrollTop || 0;
      const focused = document.activeElement;
      const focusSelector = focused?.dataset?.vmAction
        ? `[data-vm-action="${focused.dataset.vmAction}"][data-vmid="${focused.dataset.vmid}"]`
        : focused?.dataset?.standAction ? `[data-stand-action="${focused.dataset.standAction}"]`
        : focused?.dataset?.loadStandCredentials ? `[data-load-stand-credentials="${focused.dataset.loadStandCredentials}"]`
        : focused?.dataset?.copyStandAccess ? `[data-copy-stand-access="${focused.dataset.copyStandAccess}"]`
        : focused?.dataset?.deleteStand ? `[data-delete-stand="${focused.dataset.deleteStand}"]`
        : focused?.dataset?.editStand ? `[data-edit-stand="${focused.dataset.editStand}"]` : "";
      if (bodyNode) bodyNode.innerHTML = body;
      if (footerNode) footerNode.innerHTML = footer;
      if (bodyNode) bodyNode.scrollTop = scrollTop;
      if (focusSelector) existing.querySelector(focusSelector)?.focus({ preventScroll: true });
    } else {
      activeStandDetailId = Number(stand.id);
      showModal({ title: importedPool ? `Пул ${stand.pool_id}` : "Карточка стенда", subtitle: importedPool ? `Подключён к Deployer · ID ${stand.id}` : `ID ${stand.id} · управляется Deployer`, body, footer, size: "large", className: "stand-detail-modal" });
      modalRoot.querySelector(".modal")?.setAttribute("data-stand-detail-id", String(stand.id));
    }
  }

  async function openStandDetail(id, { requestCredentials = false } = {}) {
    stopStandDetailPolling();
    activeStandDetailId = Number(id);
    showModal({ title: "Загрузка стенда…", body: `<div class="detail-loading"><span class="spinner"></span><p>Получаем машины и доступы</p></div>`, size: "large", className: "stand-detail-modal" });
    const requestToken = activeModalToken;
    try {
      const stand = await api(`/api/stands/${id}`);
      if (!requestCredentials && (requestToken !== activeModalToken || activeStandDetailId !== Number(id) || !modalRoot.innerHTML)) return;
      let credentials = new Map();
      let credentialState = "loaded";
      let credentialError = "";
      try {
        const payload = await api(`/api/stands/${id}/credentials`, { promptAdmin: requestCredentials });
        credentials = normalizeVmCredentials(payload);
      } catch (error) {
        credentialState = error.status === 401 ? "locked" : "error";
        credentialError = error.message;
      }
      if (!requestCredentials && (requestToken !== activeModalToken || activeStandDetailId !== Number(id) || !modalRoot.innerHTML)) return;
      renderStandDetailModal(stand, credentials, credentialState, credentialError);
      if (standNeedsLivePolling(stand)) scheduleStandDetailPoll(Number(id), credentials, credentialState, credentialError);
    } catch (error) {
      if (!requestCredentials && requestToken !== activeModalToken) return;
      closeModal();
      toast(error.message, "error");
    }
  }

  function standNeedsLivePolling(stand) {
    return ["provisioning", "resetting"].includes(stand?.status)
      || stand?.check_status === "running"
      || (stand?.vms || []).some(vm => vm.check_status === "running")
      || Boolean(stand?.bulk_rollback_pending)
      || state.bulkRollbackPending.has(Number(stand?.id));
  }

  function settleBulkRollbackPending(stand) {
    const id = Number(stand?.id);
    const pending = state.bulkRollbackPending.get(id);
    if (!pending) return;
    if (stand.status === "resetting") {
      pending.seenActive = true;
      return;
    }
    if (stand.bulk_rollback_pending) return;
    const changedSinceScheduling = String(stand.updated_at || "") !== pending.baselineUpdatedAt;
    if (pending.seenActive || changedSinceScheduling || stand.bulk_rollback_pending === false) {
      state.bulkRollbackPending.delete(id);
    }
  }

  function syncOperationPolling() {
    const hasActiveOperations = (state.data?.stands || []).some(
      stand => standNeedsLivePolling(stand) && Number(stand.id) !== activeStandDetailId,
    );
    if (!hasActiveOperations && operationPollTimer) {
      window.clearTimeout(operationPollTimer);
      operationPollTimer = null;
      return;
    }
    if (hasActiveOperations && !operationPollTimer && !operationPollBusy) {
      operationPollTimer = window.setTimeout(pollActiveOperations, 1400);
    }
  }

  async function pollActiveOperations() {
    operationPollTimer = null;
    if (document.hidden) return;
    if (operationPollBusy || !state.data) { syncOperationPolling(); return; }
    const targets = state.data.stands.filter(stand => standNeedsLivePolling(stand) && Number(stand.id) !== activeStandDetailId);
    if (!targets.length) { syncOperationPolling(); return; }
    operationPollBusy = true;
    try {
      const updates = await Promise.allSettled(targets.map(stand => api(`/api/stands/${stand.id}`, { promptAdmin: false })));
      updates.forEach((result, index) => {
        if (result.status !== "fulfilled") return;
        const stand = result.value;
        settleBulkRollbackPending(stand);
        const stateIndex = state.data.stands.findIndex(item => Number(item.id) === Number(stand.id));
        if (stateIndex >= 0) state.data.stands[stateIndex] = { ...state.data.stands[stateIndex], ...stand };
        if (stateIndex >= 0 && state.route === "stands") {
          const currentRow = document.querySelector(`tr[data-stand-detail="${Number(stand.id)}"]`);
          if (currentRow) {
            const container = document.createElement("tbody");
            container.innerHTML = standRow(state.data.stands[stateIndex]);
            currentRow.replaceWith(container.firstElementChild);
          }
        }
      });
      updateShell();
    } finally {
      operationPollBusy = false;
      syncOperationPolling();
    }
  }

  function stopStandDetailPolling() {
    if (standDetailPollTimer) window.clearTimeout(standDetailPollTimer);
    standDetailPollTimer = null;
    activeStandDetailId = null;
    syncOperationPolling();
  }

  function scheduleStandDetailPoll(id, credentials, credentialState, credentialError) {
    if (standDetailPollTimer) window.clearTimeout(standDetailPollTimer);
    standDetailPollTimer = window.setTimeout(async () => {
      standDetailPollTimer = null;
      if (activeStandDetailId !== Number(id) || !modalRoot.querySelector(`.modal[data-stand-detail-id="${Number(id)}"]`)) return;
      try {
        const stand = await api(`/api/stands/${id}`, { promptAdmin: false });
        settleBulkRollbackPending(stand);
        let nextCredentials = credentials;
        let nextCredentialState = credentialState;
        let nextCredentialError = credentialError;
        if (credentialState === "loaded") {
          try {
            nextCredentials = normalizeVmCredentials(await api(`/api/stands/${id}/credentials`, { promptAdmin: false }));
            nextCredentialError = "";
          } catch (error) {
            nextCredentialError = error.message;
          }
        }
        const index = state.data?.stands?.findIndex(item => Number(item.id) === Number(id)) ?? -1;
        if (index >= 0) state.data.stands[index] = { ...state.data.stands[index], ...stand };
        renderStandDetailModal(stand, nextCredentials, nextCredentialState, nextCredentialError, { updateExisting: true });
        if (standNeedsLivePolling(stand)) {
          scheduleStandDetailPoll(id, nextCredentials, nextCredentialState, nextCredentialError);
        } else {
          await loadData({ silent: true });
        }
      } catch (error) {
        if (activeStandDetailId === Number(id)) standDetailPollTimer = window.setTimeout(() => scheduleStandDetailPoll(id, credentials, credentialState, credentialError), 1800);
      }
    }, 1000);
  }

  async function openRollbackStartModal(id) {
    let stand = standCredentialCache.get(Number(id))?.stand;
    if (!stand?.vms) stand = await api(`/api/stands/${id}`);
    const vmCount = stand.vms.length;
    const body = `<div class="danger-confirm compact"><span>${icon("refresh")}</span><h3>Вернуть стенд к snapshot start?</h3><p>Все изменения внутри ${vmCount} VM после первоначального развёртывания будут безвозвратно потеряны. Машины остановятся, откатятся и автоматически запустятся снова.</p><div class="alert alert--info">${icon("lock")}<div><strong>Доступы сохранятся.</strong><br>После запуска dashboard повторно применит текущие логины и пароли.</div></div></div>`;
    showModal({
      title: "Возврат к изначальному состоянию",
      subtitle: stand.name,
      body,
      footer: `<button class="button button--danger" id="rollback-start-confirm" type="button">${icon("refresh")}Вернуть все VM к start</button>`,
    });
    const button = modalRoot.querySelector("#rollback-start-confirm");
    button?.addEventListener("click", async () => {
      if (button.dataset.busy === "true") return;
      button.dataset.busy = "true";
      button.disabled = true;
      button.innerHTML = `${icon("refresh")}Запускаем возврат…`;
      try {
        const result = await api(`/api/stands/${id}/actions`, { method: "POST", body: { action: "rollback_start" } });
        toast(result.message || "Возврат к snapshot start запущен");
        closeModal();
        await loadData({ silent: true });
        await openStandDetail(id);
      } catch (error) {
        button.disabled = false;
        delete button.dataset.busy;
        button.innerHTML = `${icon("refresh")}Вернуть все VM к start`;
        toast(error.message, "error");
      }
    });
  }

  function openVmRollbackStartModal(standId, vmid, vmName) {
    const body = `<div class="danger-confirm compact"><span>${icon("refresh")}</span><h3>Вернуть VM к snapshot start?</h3><p>Все изменения внутри <strong>${escapeHtml(vmName)}</strong> после первоначального snapshot будут безвозвратно потеряны. Остальные VM этого стенда не изменятся.</p><div class="alert alert--info">${icon("lock")}<div><strong>Текущий пароль будет сохранён.</strong><br>VM остановится, откатится, запустится снова, после чего dashboard повторно применит сохранённый пароль.</div></div></div>`;
    showModal({
      title: "Вернуть VM к изначальному состоянию",
      subtitle: `${vmName} · VMID ${vmid}`,
      body,
      footer: `<button class="button button--danger" id="rollback-vm-confirm" type="button">${icon("refresh")}Вернуть эту VM к start</button>`,
    });
    const button = modalRoot.querySelector("#rollback-vm-confirm");
    button?.addEventListener("click", async () => {
      if (button.dataset.busy === "true") return;
      button.dataset.busy = "true";
      button.disabled = true;
      button.innerHTML = `${icon("refresh")}Запускаем возврат…`;
      try {
        const result = await api(`/api/stands/${standId}/vms/${vmid}/actions`, { method: "POST", body: { action: "rollback_start" } });
        toast(result.message || `Возврат VM ${vmid} запущен`);
        closeModal();
        await loadData({ silent: true });
        await openStandDetail(standId);
      } catch (error) {
        button.disabled = false;
        delete button.dataset.busy;
        button.innerHTML = `${icon("refresh")}Вернуть эту VM к start`;
        toast(error.message, "error");
      }
    });
  }

  function openRollbackAllStandsModal() {
    const managed = (state.data?.stands || []).filter(stand => stand.origin !== "imported");
    const body = `<div class="danger-confirm compact"><span>${icon("refresh")}</span><h3>Вернуть все стенды к snapshot start?</h3><p>Dashboard последовательно сбросит все подготовленные управляемые стенды. Все изменения после первоначальных snapshots будут безвозвратно потеряны.</p><div class="alert alert--warning">${icon("alert")}<div><strong>Проверка выполняется на сервере.</strong><br>Занятые стенды, импортированные pools, стенды без snapshot start или сохранённого пароля будут пропущены. Подходящих по текущему состоянию: до ${managed.length}.</div></div><label class="field"><span class="field-label">Для подтверждения введите СБРОСИТЬ</span><input class="input mono" id="rollback-all-confirm-text" autocomplete="off" spellcheck="false" placeholder="СБРОСИТЬ"></label></div>`;
    showModal({
      title: "Массовый возврат стендов",
      subtitle: "Необратимая операция",
      body,
      footer: `<button class="button button--danger" id="rollback-all-confirm" type="button" disabled>${icon("refresh")}Вернуть все стенды</button>`,
    });
    const input = modalRoot.querySelector("#rollback-all-confirm-text");
    const button = modalRoot.querySelector("#rollback-all-confirm");
    input?.addEventListener("input", () => { button.disabled = input.value.trim() !== "СБРОСИТЬ"; });
    button?.addEventListener("click", async () => {
      if (button.dataset.busy === "true" || input.value.trim() !== "СБРОСИТЬ") return;
      button.dataset.busy = "true";
      button.disabled = true;
      button.innerHTML = `${icon("refresh")}Формируем очередь…`;
      try {
        const result = await api("/api/stands/actions", { method: "POST", body: { action: "rollback_start_all" } });
        (result.scheduled || []).forEach(id => {
          const baseline = state.data?.stands?.find(stand => Number(stand.id) === Number(id));
          state.bulkRollbackPending.set(Number(id), {
            seenActive: false,
            baselineUpdatedAt: String(baseline?.updated_at || ""),
          });
        });
        closeModal();
        toast(`${result.message}. Пропущено: ${Number(result.skipped_count || 0)}`, result.skipped_count ? "warning" : "success", 6500);
        await loadData();
        syncOperationPolling();
      } catch (error) {
        button.disabled = false;
        delete button.dataset.busy;
        button.innerHTML = `${icon("refresh")}Вернуть все стенды`;
        toast(error.message, "error");
      }
    });
  }

  async function performStandAction(id, action) {
    if (action === "password") { openPasswordModal(id); return; }
    if (action === "rollback_start") { await openRollbackStartModal(id); return; }
    const labels = { start: "Запускаем стенд…", stop: "Останавливаем стенд…", restart: "Перезапускаем стенд…", snapshot: "Создаём снимок…", run_check: "Запускаем автопроверку…" };
    try {
      const payload = { action };
      if (action === "snapshot") payload.name = `manual-${new Date().toISOString().slice(0, 19).replaceAll(":", "-")}`;
      const result = await api(`/api/stands/${id}/actions`, { method: "POST", body: payload });
      toast(result.message || labels[action]);
      if (action === "run_check") { await loadData({ silent: true }); await openStandDetail(id); }
      else { closeModal(); await loadData(); }
    } catch (error) { toast(error.message, "error"); }
  }

  async function performVmAction(standId, vmid, action, vmName) {
    if (action === "password") {
      openPasswordModal(standId, { vmid, vmName });
      return;
    }
    if (action === "rollback_start") {
      openVmRollbackStartModal(standId, vmid, vmName);
      return;
    }
    const labels = { snapshot: `Создаём снимок VM «${vmName}»…`, run_check: `Автопроверка VM «${vmName}» запущена…` };
    try {
      const payload = { action };
      if (action === "snapshot") payload.name = `manual-${new Date().toISOString().slice(0, 19).replaceAll(":", "-")}`;
      const result = await api(`/api/stands/${standId}/vms/${vmid}/actions`, { method: "POST", body: payload });
      toast(result.message || labels[action] || "Операция выполнена");
      await loadData({ silent: true });
      await openStandDetail(standId);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function openStandEditor(id) {
    const stand = state.data.stands.find(item => item.id === Number(id));
    if (!stand) return;
    const body = `<form id="stand-edit-form" class="form-grid">
      <label class="field field--full"><span class="field-label">Название стенда</span><input class="input" name="name" value="${escapeHtml(stand.name)}" required></label>
      <label class="field"><span class="field-label">Ответственный</span><input class="input" name="owner" value="${escapeHtml(stand.owner)}"></label>
      <label class="field"><span class="field-label">Диапазон IPAM</span><input class="input mono" name="ip_range" value="${escapeHtml(stand.ip_range)}" readonly><small class="field-hint">Адреса уже зарезервированы. Для другой сети разверните новый стенд.</small></label>
      <div class="field field--full"><div class="alert alert--info">${icon("info")} Стенд работает бессрочно. Можно изменить название и ответственного; адреса VM остаются под управлением IPAM.</div></div>
    </form>`;
    showModal({ title: "Параметры стенда", subtitle: stand.pool_id, body, footer: `<button class="button" data-close-modal type="button">Отмена</button><button class="button button--primary" type="submit" form="stand-edit-form">${icon("check")}Сохранить</button>` });
    modalRoot.querySelector("#stand-edit-form").addEventListener("submit", async event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget).entries());
      try {
        await api(`/api/stands/${id}`, { method: "PATCH", body: values });
        closeModal(); toast("Параметры стенда обновлены"); await loadData();
      } catch (error) { toast(error.message, "error"); }
    });
  }

  function openPasswordModal(id, vmTarget = null) {
    const stand = state.data.stands.find(item => item.id === Number(id));
    const singleVm = Number.isInteger(Number(vmTarget?.vmid));
    const targetName = singleVm ? (vmTarget.vmName || `VMID ${vmTarget.vmid}`) : "всех VM стенда";
    const body = `<form id="password-form"><div class="credential-intro"><span>${icon("lock")}</span><div><h3>${singleVm ? `Только ${escapeHtml(targetName)}` : "Ротация на всех VM"}</h3><p>Новый пароль будет установлен через QEMU Guest Agent и показан только один раз.</p></div></div><div class="form-grid"><label class="field"><span class="field-label">Системный пользователь</span><input class="input mono" name="username" value="root" required><small class="field-hint">Для входа в web UI используется логин root@pam</small></label><label class="field"><span class="field-label">Режим</span><select class="input" id="password-mode"><option value="generate">Сгенерировать безопасный</option><option value="custom">Задать вручную</option></select></label><label class="field field--full" id="custom-password-field" hidden><span class="field-label">Новый пароль</span><input class="input mono" type="password" name="password" minlength="10" autocomplete="new-password"><small class="field-hint">Dashboard не вводит короткий искусственный лимит; технический предел Proxmox — 1024 символа</small></label></div><div class="alert alert--warning">${icon("alert")} Новый пароль потребуется при следующей авторизации в веб-интерфейсе этой ${singleVm ? "VM" : "группы VM"}.</div></form>`;
    const footer = `<button class="button" data-close-modal type="button">Отмена</button><button class="button button--primary" type="submit" form="password-form">${icon("lock")}Сменить пароль</button>`;
    showModal({ title: singleVm ? "Сменить пароль VM" : "Сменить пароль стенда", subtitle: singleVm ? `${stand?.name || `Стенд #${id}`} · ${targetName}` : stand?.name || `Стенд #${id}`, body, footer });
    const mode = modalRoot.querySelector("#password-mode");
    mode.addEventListener("change", () => {
      const customField = modalRoot.querySelector("#custom-password-field");
      const customInput = customField.querySelector("input[name=password]");
      customField.hidden = mode.value !== "custom";
      customInput.required = mode.value === "custom";
    });
    modalRoot.querySelector("#password-form").addEventListener("submit", async event => {
      event.preventDefault(); const submit = modalRoot.querySelector("button[type=submit]"); submit.disabled = true;
      const values = Object.fromEntries(new FormData(event.currentTarget).entries());
      if (mode.value === "generate") delete values.password;
      try {
        const endpoint = singleVm ? `/api/stands/${id}/vms/${vmTarget.vmid}/actions` : `/api/stands/${id}/actions`;
        const result = await api(endpoint, { method: "POST", body: { action: "rotate_password", ...values } });
        showCredentialResult(result.credential, singleVm ? `${stand?.name || `Стенд #${id}`} · ${targetName}` : stand?.name || `Стенд #${id}`); await loadData({ silent: true });
      } catch (error) { submit.disabled = false; toast(error.message, "error"); }
    });
  }

  function showCredentialResult(credential, standName) {
    const accessUsername = credential.access_username || credential.web_username || (credential.username === "root" ? "root@pam" : credential.username);
    const body = `<div class="credential-result"><div class="credential-result__success">${icon("check")}</div><h3>Пароль успешно обновлён</h3><p>Скопируйте данные сейчас. Для входа в web UI используйте указанный логин.</p><div class="credential-box"><div><span>Логин web UI</span><strong class="mono">${escapeHtml(accessUsername)}</strong></div><div><span>Новый пароль</span><strong class="mono" id="one-time-password">••••••••••••••••</strong><button type="button" data-reveal-password data-value="${escapeHtml(credential.password)}">${icon("eye")}Показать</button></div></div><button class="button button--large" type="button" data-copy-credential data-user="${escapeHtml(accessUsername)}" data-password="${escapeHtml(credential.password)}">${icon("copy")}Скопировать доступ</button><div class="one-time-note">${icon("shield")} Данные также доступны из защищённой карточки VM</div></div>`;
    showModal({ title: "Новые учётные данные", subtitle: standName, body, footer: `<button class="button button--primary" data-close-modal type="button">Готово</button>` });
  }

  function openRunCheckModal() {
    const selected = state.data.blueprints.find(item => item.id === state.selectedBlueprintId);
    const stands = state.data.stands.filter(item => item.status === "running" && (!selected || item.blueprint_id === selected.id));
    if (!stands.length) { toast("Нет работающего стенда с выбранным сценарием", "warning"); return; }
    const body = `<form id="run-check-form"><label class="field"><span class="field-label">Целевой стенд</span><select class="input" name="stand_id">${stands.map(stand => `<option value="${stand.id}">${escapeHtml(stand.name)} · ${stand.vm_count} VM</option>`).join("")}</select></label><div class="run-check-summary"><span>${icon("terminal")}</span><div><strong>${escapeHtml(selected?.name || "Автопроверка")}</strong><small>${selected?.autocheck_script.split("\n").length || 0} строк · таймаут 120 секунд на VM</small></div></div><div class="alert alert--info">${icon("info")} Редактируемый код выполняется внутри гостевых машин, а результат сохраняется в журнале.</div></form>`;
    showModal({ title: "Запустить автопроверку", subtitle: selected?.code || "Выбранный сценарий", body, footer: `<button class="button" data-close-modal type="button">Отмена</button><button class="button button--primary" type="submit" form="run-check-form">${icon("power")}Запустить</button>` });
    modalRoot.querySelector("#run-check-form").addEventListener("submit", async event => {
      event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget).entries());
      const submit = modalRoot.querySelector('button[type="submit"]'); submit.disabled = true;
      try { await api("/api/checks/run", { method: "POST", body: values }); closeModal(); toast("Автопроверка запущена"); await loadData(); }
      catch (error) { submit.disabled = false; toast(error.message, "error"); }
    });
  }

  function openCheckDetail(runId) {
    const run = state.data.checks.find(item => item.id === Number(runId));
    if (!run) return;
    const details = run.details || [];
    const scoreTone = run.status === "running" ? "pending" : run.score >= 90 ? "good" : run.score >= 70 ? "warn" : "bad";
    const body = `<div class="check-result-head"><div class="check-score-ring check-score-ring--${scoreTone}" style="--score:${clamp(run.score)}%"><strong>${run.score == null ? "…" : `${run.score}%`}</strong></div><div><h3>${escapeHtml(run.stand_name)}</h3><p>${run.status === "running" ? "Проверка выполняется" : `${run.passed} из ${run.total} проверок пройдено`}</p>${statusChip(run.status === "running" ? "checking" : run.status)}</div></div>
      <div class="check-result-list">${details.map(item => `<div class="check-result-item ${item.ok ? "is-ok" : "is-failed"}"><span>${icon(item.ok ? "check" : "x")}</span><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.message || (item.ok ? "Условие выполнено" : "Требуется исправление"))}</small></div><time>${item.duration || 0} мс</time></div>`).join("") || `<div class="detail-loading"><span class="spinner"></span><p>Ожидаем результаты от VM…</p></div>`}</div><div class="console-output"><div><span class="console-prompt">$</span> demoops check --run ${run.id}</div><pre>${escapeHtml(run.output || "Проверка выполняется…")}</pre></div>`;
    showModal({ title: "Результаты автопроверки", subtitle: `${run.blueprint_code || ""} · ${dateTime(run.started_at)}`, body, footer: `<button class="button" data-close-modal type="button">Закрыть</button>`, size: "wide" });
  }

  async function saveAutocheck() {
    const editor = document.querySelector("#autocheck-editor");
    if (!editor || !state.selectedBlueprintId) return false;
    const submitted = editor.value;
    try {
      await api(`/api/blueprints/${state.selectedBlueprintId}`, { method: "PATCH", body: { autocheck_script: submitted } });
      const unchanged = editor.value === submitted;
      state.editorDirty = !unchanged;
      toast(unchanged ? "Скрипт автопроверки сохранён" : "Версия сохранена; в редакторе есть новые изменения");
      await loadData({ silent: true });
      const indicator = document.querySelector("#editor-dirty-indicator");
      if (indicator) { indicator.textContent = unchanged ? "Сохранено" : "Есть изменения"; indicator.className = unchanged ? "editor-saved" : "editor-unsaved"; }
      return unchanged;
    } catch (error) { toast(error.message, "error"); return false; }
  }

  async function confirmDeleteStand(id) {
    const stand = state.data.stands.find(item => item.id === Number(id));
    const imported = stand?.origin === "imported";
    const existingPool = stand?.origin === "existing";
    const confirmationTarget = imported ? stand?.pool_id : stand?.name;
    const body = `<div class="danger-confirm"><span>${icon(imported ? "info" : "trash")}</span><h3>${imported ? "Убрать pool из списка?" : "Удалить стенд безвозвратно?"}</h3><p>${imported ? `Pool <strong class="mono">${escapeHtml(stand?.pool_id)}</strong> и все его VM останутся в Proxmox. Из Deployer удалится только подключение и история проверок.` : existingPool ? `Будут удалены только ${stand?.vm_count || 0} VM этого стенда и их диски. Существующий pool <strong class="mono">${escapeHtml(stand?.pool_id)}</strong> и остальные его ресурсы сохранятся.` : `Будут удалены пул <strong class="mono">${escapeHtml(stand?.pool_id)}</strong>, ${stand?.vm_count || 0} VM, их диски и история проверок.`}</p><label class="field"><span class="field-label">${imported ? "Введите название pool" : "Введите название стенда"} для подтверждения</span><input class="input" id="delete-confirm-name" autocomplete="off" placeholder="${escapeHtml(confirmationTarget)}"></label></div>`;
    showModal({ title: imported ? "Удаление pool из списка" : "Удаление стенда", subtitle: imported || existingPool ? "Сам Proxmox pool сохранится" : "Необратимая операция", body, footer: `<button class="button" data-close-modal type="button">Отмена</button><button class="button ${imported ? "" : "button--danger"}" id="delete-stand-confirm" type="button" disabled>${icon(imported ? "info" : "trash")}${imported ? "Убрать из списка" : "Удалить навсегда"}</button>` });
    const input = modalRoot.querySelector("#delete-confirm-name"), button = modalRoot.querySelector("#delete-stand-confirm");
    input.addEventListener("input", () => { button.disabled = input.value !== confirmationTarget; });
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await api(`/api/stands/${id}`, { method: "DELETE" }); state.ipam = null; closeModal(); toast(imported ? "Pool убран из списка; ресурсы Proxmox сохранены" : existingPool ? "Стенд удалён; существующий pool сохранён" : "Стенд удалён", imported ? "info" : "warning"); await loadData(); }
      catch (error) { button.disabled = false; toast(error.message, "error"); }
    });
  }

  async function confirmDeleteBlueprint(id) {
    const blueprint = state.data.blueprints.find(item => item.id === Number(id));
    showModal({ title: "Удалить сценарий?", subtitle: blueprint?.code || "", body: `<div class="danger-confirm compact"><span>${icon("trash")}</span><h3>${escapeHtml(blueprint?.name)}</h3><p>Удаление возможно только если сценарий не используется ни одним стендом.</p></div>`, footer: `<button class="button" data-close-modal>Отмена</button><button class="button button--danger" id="delete-blueprint-confirm">${icon("trash")}Удалить</button>` });
    modalRoot.querySelector("#delete-blueprint-confirm").addEventListener("click", async () => {
      try { await api(`/api/blueprints/${id}`, { method: "DELETE" }); closeModal(); toast("Сценарий удалён", "warning"); await loadData(); }
      catch (error) { toast(error.message, "error"); }
    });
  }

  function showNotifications() {
    if (!state.data) { toast("События ещё загружаются", "warning"); return; }
    const body = `<div class="notification-list">${state.data.activity.slice(0, 12).map(activityItem).join("")}</div>`;
    showModal({ title: "Центр событий", subtitle: `${state.data.overview.attention} требуют внимания`, body, footer: `<button class="button" data-close-modal>Закрыть</button>`, size: "wide" });
  }

  function navigate(route) {
    if (!routes.includes(route)) return;
    if (route === state.route) {
      document.body.classList.remove("sidebar-open");
      document.querySelector("#sidebar-toggle")?.setAttribute("aria-expanded", "false");
      return;
    }
    if (state.editorDirty && state.route === "checks" && route !== "checks" && !window.confirm("В скрипте есть несохранённые изменения. Покинуть редактор?")) {
      window.history.replaceState(null, "", `#${state.route}`);
      return;
    }
    state.editorDirty = false;
    if (route !== "infrastructure" && webActivityPollTimer) {
      window.clearTimeout(webActivityPollTimer);
      webActivityPollTimer = null;
    }
    state.route = route;
    if (route === "ipam") state.ipam = null;
    window.location.hash = route;
    render();
    document.body.classList.remove("sidebar-open");
    document.querySelector("#sidebar-toggle")?.setAttribute("aria-expanded", "false");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function closeSidebar() {
    document.body.classList.remove("sidebar-open");
    document.querySelector("#sidebar-toggle")?.setAttribute("aria-expanded", "false");
  }

  function standAccessText(standId) {
    const cached = standCredentialCache.get(Number(standId));
    if (!cached) return { text: "", complete: 0, total: 0 };
    let complete = 0;
    const lines = (cached.stand.vms || []).map(vm => {
      const credential = cached.credentials.get(Number(vm.vmid));
      const username = webUsername(credential, vm);
      const password = credential?.password || credential?.access_password || "пароль не выдан";
      if (password !== "пароль не выдан") complete += 1;
      return `${vm.ip || "IP не назначен"} | ${username} | ${password}`;
    });
    return { text: lines.join("\n"), complete, total: lines.length };
  }

  async function openVmWeb(button) {
    const accessUrl = safeAccessUrl(button.dataset.url);
    if (!accessUrl) {
      toast("Web URL этой VM ещё не получен", "warning");
      return;
    }
    const username = button.dataset.user || "root@pam";
    const password = button.dataset.password || "";
    const windowName = `demoops_vm_${String(button.dataset.vmid || Date.now()).replace(/[^0-9A-Za-z_]/g, "")}`;
    const ticketUrl = new URL("/api2/html/access/ticket", accessUrl).href;
    const body = `<div class="tls-access-guide">
      <div class="tls-access-guide__head">${icon("shield")}<div><h3>${escapeHtml(button.dataset.vmName || "Web-интерфейс стенда")}</h3><p class="mono">${escapeHtml(accessUrl)}</p></div></div>
      <div class="alert alert--warning">${icon("alert")}<div><strong>Первое открытие может остановиться на предупреждении сертификата.</strong><br>Эту системную страницу браузера нельзя нажать из дашборда автоматически.</div></div>
      <ol class="tls-steps"><li><span>1</span><div><strong>Откройте адрес и примите риск</strong><small>Firefox: «Дополнительно» → «Принять риск и продолжить». Chrome/Edge: «Дополнительные» → «Перейти на сайт (небезопасно)».</small></div></li><li><span>2</span><div><strong>${password ? "Вернитесь сюда и нажмите «Войти автоматически»" : "Войдите вручную"}</strong><small>${password ? "Дашборд отправит логин и пароль штатной HTML-формой Proxmox в ту же вкладку." : `Используйте логин ${escapeHtml(username)}. Сначала загрузите пароль в карточке VM.`}</small></div></li><li><span>3</span><div><strong>Нажмите «Открыть интерфейс»</strong><small>Вкладка откроется повторно уже с установленной сессией. Если политика браузера заблокировала cookie, используйте кнопки копирования ниже.</small></div></li></ol>
      <div class="tls-credentials"><div><span>Логин web UI</span><strong class="mono">${escapeHtml(username)}</strong><button class="button button--small" type="button" data-copy-vm-login data-value="${escapeHtml(username)}">${icon("copy")}Копировать логин</button></div><div><span>Пароль</span><strong class="mono" data-vm-secret>${password ? "••••••••••••" : "Не загружен"}</strong>${password ? `<div><button class="button button--small" type="button" data-reveal-vm-password data-password="${escapeHtml(password)}">${icon("eye")}Показать</button><button class="button button--small" type="button" data-copy-vm-password data-value="${escapeHtml(password)}">${icon("copy")}Копировать пароль</button></div>` : ""}</div></div>
      ${password ? `<button class="button tls-copy-pair" type="button" data-copy-vm-credential data-user="${escapeHtml(username)}" data-password="${escapeHtml(password)}">${icon("copy")}Скопировать логин и пароль</button>` : ""}
    </div>`;
    const footer = `<button class="button" type="button" data-launch-vm-web data-url="${escapeHtml(accessUrl)}" data-window="${escapeHtml(windowName)}" data-password="${escapeHtml(password)}">1. Открыть и принять риск</button>${password ? `<button class="button button--primary" type="button" data-auto-login-vm data-url="${escapeHtml(ticketUrl)}" data-window="${escapeHtml(windowName)}" data-user="${escapeHtml(username)}" data-password="${escapeHtml(password)}">${icon("lock")}2. Войти автоматически</button>` : ""}<button class="button" type="button" data-launch-vm-web data-url="${escapeHtml(accessUrl)}" data-window="${escapeHtml(windowName)}" data-copy-password="false">${icon("external")}3. Открыть интерфейс</button>`;
    showModal({ title: "Переход к стенду", subtitle: "Вход через web-интерфейс Proxmox", body, footer, size: "wide", className: "vm-access-modal" });
  }

  document.addEventListener("click", async event => {
    const target = event.target.closest("button, a, tr");
    if (!target) return;
    if (target.matches(".nav-link[data-route], .brand[data-route]")) { event.preventDefault(); navigate(target.dataset.route); }
    else if (target.dataset.navigate) navigate(target.dataset.navigate);
    else if (target.dataset.rollbackAllStands !== undefined) openRollbackAllStandsModal();
    else if (target.dataset.importPool !== undefined) openImportPoolModal();
    else if (target.matches("[data-open-deploy]")) openDeployWizard(target.dataset.openDeploy || null);
    else if (target.matches("[data-refresh-ipam]")) { state.ipam = null; renderIpam(); await loadIpam({ force: true }); }
    else if (target.matches("[data-refresh-web-activity]")) await loadWebActivity({ force: true });
    else if (target.matches("[data-refresh]")) loadData();
    else if (target.dataset.standFilter) { state.standFilter = target.dataset.standFilter; renderStands(); }
    else if (target.dataset.standDetail) openStandDetail(Number(target.dataset.standDetail));
    else if (target.dataset.loadStandCredentials) openStandDetail(Number(target.dataset.loadStandCredentials), { requestCredentials: true });
    else if (target.matches("[data-open-vm-stand]")) await openVmWeb(target);
    else if (target.matches("[data-launch-vm-web]")) {
      const url = safeAccessUrl(target.dataset.url);
      if (url) window.open(url, target.dataset.window || "_blank");
      if (target.dataset.password && target.dataset.copyPassword !== "false") { await copyText(target.dataset.password); toast("Web UI открыт, пароль также скопирован. Примите риск сертификата и вернитесь к шагу 2.", "info"); }
    }
    else if (target.matches("[data-auto-login-vm]")) {
      const ticketUrl = safeAccessUrl(target.dataset.url);
      if (!ticketUrl || !target.dataset.user || !target.dataset.password) { toast("Нет данных для автоматического входа", "warning"); return; }
      const form = document.createElement("form");
      form.method = "POST"; form.action = ticketUrl; form.target = target.dataset.window || "_blank"; form.hidden = true;
      [["username", target.dataset.user], ["password", target.dataset.password]].forEach(([name, value]) => {
        const input = document.createElement("input"); input.type = "hidden"; input.name = name; input.value = value; form.append(input);
      });
      document.body.append(form); form.submit(); form.remove();
      toast("Данные отправлены в Proxmox. После ответа нажмите шаг 3 — «Открыть интерфейс».", "info");
    }
    else if (target.dataset.vmAction) {
      if (target.dataset.busy === "true") return;
      target.dataset.busy = "true"; target.disabled = true;
      try { await performVmAction(Number(target.dataset.standId), Number(target.dataset.vmid), target.dataset.vmAction, target.dataset.vmName || `VMID ${target.dataset.vmid}`); }
      finally { if (target.isConnected) { target.disabled = false; delete target.dataset.busy; } }
    }
    else if (target.dataset.blueprintNew !== undefined) openBlueprintEditor();
    else if (target.dataset.blueprintEdit) openBlueprintEditor(Number(target.dataset.blueprintEdit));
    else if (target.dataset.blueprintDuplicate) {
      try { await api(`/api/blueprints/${target.dataset.blueprintDuplicate}/duplicate`, { method: "POST", body: {} }); toast("Копия сценария создана"); await loadData(); } catch (error) { toast(error.message, "error"); }
    }
    else if (target.dataset.blueprintDelete) confirmDeleteBlueprint(Number(target.dataset.blueprintDelete));
    else if (target.dataset.selectCheck) { if (state.editorDirty && !window.confirm("Отменить несохранённые изменения?")) return; state.editorDirty = false; state.selectedBlueprintId = Number(target.dataset.selectCheck); renderChecks(); }
    else if (target.matches("[data-save-check]")) saveAutocheck();
    else if (target.matches("[data-run-check]")) { if (!state.editorDirty || await saveAutocheck()) openRunCheckModal(); }
    else if (target.dataset.checkDetail) openCheckDetail(Number(target.dataset.checkDetail));
    else if (target.matches("[data-copy-editor]")) { const editor = document.querySelector("#autocheck-editor"); if (editor) { await copyText(editor.value); toast("Код скопирован"); } }
    else if (target.matches("[data-expand-editor]")) document.querySelector(".editor-shell")?.classList.toggle("is-expanded");
    else if (target.dataset.standAction) {
      if (target.dataset.busy === "true") return;
      target.dataset.busy = "true"; target.disabled = true;
      try { await performStandAction(Number(target.dataset.standId), target.dataset.standAction); }
      finally { if (target.isConnected) { target.disabled = false; delete target.dataset.busy; } }
    }
    else if (target.dataset.editStand) openStandEditor(Number(target.dataset.editStand));
    else if (target.dataset.deleteStand) confirmDeleteStand(Number(target.dataset.deleteStand));
    else if (target.matches("[data-copy-stand-access]")) {
      const standId = Number(target.dataset.copyStandAccess);
      let access = standAccessText(standId);
      if (!access.total || access.complete < access.total) {
        try {
          const cached = standCredentialCache.get(standId);
          const stand = cached?.stand || await api(`/api/stands/${standId}`, { promptAdmin: false });
          const credentials = normalizeVmCredentials(await api(`/api/stands/${standId}/credentials`));
          standCredentialCache.set(standId, { stand, credentials });
          access = standAccessText(standId);
          if (activeStandDetailId === standId) {
            renderStandDetailModal(stand, credentials, "loaded", "", { updateExisting: true });
          }
        } catch (error) {
          toast(error.message, "error");
          return;
        }
      }
      if (!access.text) { toast("Для этого стенда пока нет VM", "warning"); return; }
      await copyText(access.text);
      toast(access.complete === access.total ? `Скопированы доступы ${access.total} VM` : `Скопировано ${access.total} строк; пароли есть у ${access.complete} VM`, access.complete === access.total ? "success" : "warning");
    }
    else if (target.matches("[data-reveal-vm-password]")) { const secret = target.closest(".vm-access, .tls-credentials")?.querySelector("[data-vm-secret]"); if (secret) secret.textContent = target.dataset.password; target.remove(); }
    else if (target.matches("[data-copy-vm-login]")) { await copyText(target.dataset.value || ""); toast("Логин скопирован"); }
    else if (target.matches("[data-copy-vm-password]")) { await copyText(target.dataset.value || ""); toast("Пароль скопирован"); }
    else if (target.matches("[data-copy-vm-credential]")) { await copyText(`${target.dataset.user}\n${target.dataset.password}`); toast("Логин и пароль VM скопированы"); }
    else if (target.matches("[data-reveal-password]")) { document.querySelector("#one-time-password").textContent = target.dataset.value; target.remove(); }
    else if (target.matches("[data-copy-credential]")) { await copyText(`${target.dataset.user}\n${target.dataset.password}`); toast("Учётные данные скопированы"); }
    else if (target.matches("[data-close-modal]")) closeModal();
    else if (target.matches("#notifications-button")) showNotifications();
  });

  async function copyText(value) {
    try { await navigator.clipboard.writeText(value); }
    catch { const area = document.createElement("textarea"); area.value = value; area.style.position = "fixed"; area.style.opacity = "0"; document.body.append(area); area.select(); document.execCommand("copy"); area.remove(); }
  }

  document.querySelector("#quick-create")?.addEventListener("click", () => openDeployWizard());
  document.querySelector("#global-refresh")?.addEventListener("click", () => loadData());
  document.querySelector("#operator-settings")?.addEventListener("click", () => requestAdminToken(true));
  document.querySelector("#sidebar-toggle")?.addEventListener("click", event => { document.body.classList.toggle("sidebar-open"); event.currentTarget.setAttribute("aria-expanded", String(document.body.classList.contains("sidebar-open"))); });
  document.querySelector("#sidebar-close")?.addEventListener("click", closeSidebar);
  document.querySelector("#sidebar-scrim")?.addEventListener("click", closeSidebar);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      if (!modalRoot.innerHTML) closeSidebar();
      return;
    }
    if (event.key === "Tab" && modalRoot.innerHTML) {
      const focusable = [...modalRoot.querySelectorAll('button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')].filter(node => node.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      syncOperationPolling();
      if (state.route === "infrastructure") loadWebActivity({ background: true });
    }
  });
  window.addEventListener("resize", () => { if (window.innerWidth > 980) closeSidebar(); });
  window.addEventListener("hashchange", () => { const route = getRoute(); if (route !== state.route) navigate(route); });
  window.addEventListener("beforeunload", event => { if (state.editorDirty) { event.preventDefault(); event.returnValue = ""; } });

  loadData();
  window.setInterval(() => { if (!document.hidden) loadData({ silent: true }); }, 30000);
})();
