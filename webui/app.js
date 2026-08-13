const availability = document.querySelector("#availability");
const state = document.querySelector("#state");
const sequence = document.querySelector("#sequence");
const replay = JSON.parse(document.querySelector("#replay-data").textContent);

availability.textContent = replay.availability;
state.textContent = replay.state || "Unavailable";
sequence.textContent = replay.sequence === undefined ? "" : `Audit sequence ${replay.sequence}`;
