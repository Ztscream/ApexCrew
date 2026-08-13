const replay = JSON.parse(document.querySelector("#replay-data").textContent);
const frames = replay.frames;
const phaseOrder = ["DRAFT", "PLANNING", "ACTIVE", "VERIFYING", "READY_FOR_APPROVAL", "COMPLETED"];
const scrub = document.querySelector('[data-control="scrub"]');
const workerFilter = document.querySelector('[data-control="worker-filter"]');
const taskFilter = document.querySelector('[data-control="task-filter"]');
const auditRows = Array.from(document.querySelectorAll("[data-audit-row]"));
let activeIndex = frames.length - 1;
let timer = null;

function setText(selector, value) {
  document.querySelector(selector).textContent = String(value);
}

function updateFilters() {
  let visibleRows = 0;
  auditRows.forEach((row) => {
    const workerMatches = workerFilter.value === "all" || row.dataset.worker === workerFilter.value;
    const taskMatches = taskFilter.value === "all" || row.dataset.task === taskFilter.value;
    row.hidden = !(workerMatches && taskMatches);
    if (!row.hidden) visibleRows += 1;
  });
  document.querySelector("#audit-empty").hidden = visibleRows !== 0;
}

function showFrame(index) {
  activeIndex = Math.max(0, Math.min(index, frames.length - 1));
  const frame = frames[activeIndex];
  scrub.value = String(frame.sequence);
  setText("#current-sequence", frame.sequence);
  setText("#event-index", String(frame.sequence).padStart(2, "0"));
  setText("#event-category", frame.category);
  setText("#event-title", frame.title);
  setText("#event-detail", frame.detail);
  setText("#event-time", frame.time);
  setText("#evidence-state", frame.evidence);
  setText("#authority-state", frame.authority);
  setText("#checks-passed", frame.checks);
  setText("#snapshot-id", frame.snapshot);
  setText("#run-state", frame.state);

  document.querySelectorAll("[data-check-threshold]").forEach((check) => {
    const passed = frame.checks >= Number.parseInt(check.dataset.checkThreshold, 10);
    const result = check.querySelector("strong");
    result.textContent = passed ? "PASS" : "PENDING";
    result.classList.toggle("is-pending", !passed);
  });

  document.querySelectorAll("[data-authority-sequence]").forEach((decision) => {
    const available = frame.sequence >= Number.parseInt(decision.dataset.authoritySequence, 10);
    decision.querySelector("strong").textContent = available ? decision.dataset.authorityResult : "pending";
    decision.classList.toggle("is-pending", !available);
  });

  const currentPhase = phaseOrder.indexOf(frame.state);
  document.querySelectorAll("[data-phase]").forEach((phase) => {
    const phaseIndex = phaseOrder.indexOf(phase.dataset.phase);
    phase.classList.toggle("is-complete", phaseIndex < currentPhase);
    phase.classList.toggle("is-current", phaseIndex === currentPhase);
  });

  auditRows.forEach((row) => {
    const sequence = Number.parseInt(row.dataset.sequence, 10);
    row.classList.toggle("is-future", sequence > frame.sequence);
    row.classList.toggle("is-selected", sequence === frame.sequence);
  });
}

function pause() {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function step() {
  if (activeIndex >= frames.length - 1) {
    pause();
    return;
  }
  showFrame(activeIndex + 1);
}

document.querySelector('[data-control="play"]').addEventListener("click", () => {
  if (activeIndex >= frames.length - 1) showFrame(0);
  if (timer === null) timer = window.setInterval(step, 1100);
});
document.querySelector('[data-control="pause"]').addEventListener("click", pause);
document.querySelector('[data-control="step"]').addEventListener("click", () => {
  pause();
  step();
});
scrub.addEventListener("input", () => {
  pause();
  showFrame(Number.parseInt(scrub.value, 10) - 1);
});
workerFilter.addEventListener("change", updateFilters);
taskFilter.addEventListener("change", updateFilters);

setText("#run-id", replay.run_id);
setText("#run-title", replay.goal);
setText("#repository", replay.repository);
setText("#plan-revision", replay.plan_revision);
showFrame(activeIndex);
updateFilters();
