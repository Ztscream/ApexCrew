const availability = document.querySelector("#availability");
const state = document.querySelector("#state");
const sequence = document.querySelector("#sequence");

fetch("/api/run", { method: "GET", credentials: "omit", cache: "no-store" })
  .then((response) => response.json())
  .then((run) => {
    availability.textContent = run.availability;
    state.textContent = run.state || "Unavailable";
    sequence.textContent = run.sequence === undefined ? "" : `Audit sequence ${run.sequence}`;
  })
  .catch(() => {
    availability.textContent = "UNAVAILABLE";
    state.textContent = "Read failed";
  });
