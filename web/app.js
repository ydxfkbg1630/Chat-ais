const streams = {
  chatgpt: document.getElementById("stream-chatgpt"),
  gemini: document.getElementById("stream-gemini"),
};

const counts = {
  chatgpt: document.getElementById("count-chatgpt"),
  gemini: document.getElementById("count-gemini"),
};

const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");

const totals = {
  chatgpt: 0,
  gemini: 0,
};

function setStatus(text, live) {
  statusText.textContent = text;
  statusDot.classList.toggle("is-live", live);
}

function formatTime(timestamp) {
  if (!timestamp) {
    return "";
  }
  return timestamp;
}

function appendMessage(event) {
  const site = event.site;
  const stream = streams[site];
  if (!stream) {
    return;
  }

  const card = document.createElement("article");
  card.className = "message";
  card.dataset.site = site;

  const meta = document.createElement("div");
  meta.className = "message__meta";
  const label = document.createElement("span");
  label.textContent = site.toUpperCase();
  const time = document.createElement("span");
  time.textContent = formatTime(event.timestamp);
  meta.append(label, time);

  const body = document.createElement("p");
  body.className = "message__text";
  body.textContent = event.text || "";

  card.append(meta, body);
  stream.appendChild(card);
  stream.scrollTop = stream.scrollHeight;

  totals[site] = (totals[site] || 0) + 1;
  counts[site].textContent = totals[site];
}

async function loadHistory() {
  try {
    const response = await fetch("/history");
    if (!response.ok) {
      return;
    }
    const history = await response.json();
    history.forEach((event) => {
      if (event.type === "message") {
        appendMessage(event);
      }
    });
  } catch (err) {
    console.error(err);
  }
}

function connect() {
  const source = new EventSource("/events");

  source.onopen = () => setStatus("Live", true);
  source.onerror = () => setStatus("Reconnecting...", false);
  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "message") {
        appendMessage(event);
      } else if (event.type === "status" && event.text) {
        setStatus(event.text, true);
      }
    } catch (err) {
      console.error(err);
    }
  };
}

setStatus("Connecting...", false);
loadHistory();
connect();
