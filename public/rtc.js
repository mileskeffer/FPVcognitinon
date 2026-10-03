// Shared helpers for the viewer and sender pages.
export async function getIceServers() {
  const res = await fetch("/api/config");
  return (await res.json()).iceServers;
}

export function openSignaling(room, role, onMessage) {
  let chain = Promise.resolve();
  let active = true;

  const deliver = (msg) => {
    chain = chain.then(() => onMessage(msg)).catch(console.error);
  };

  const poll = async () => {
    if (!active) return;
    const response = await fetch(`/api/signaling/messages?room=${encodeURIComponent(room)}&role=${role}`);
    for (const message of await response.json()) deliver(message);
    setTimeout(poll, 250);
  };

  fetch("/api/signaling/join", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ room, role }),
  })
    .then((response) => response.json())
    .then(deliver)
    .then(poll)
    .catch(console.error);

  window.addEventListener("beforeunload", () => {
    active = false;
    fetch("/api/signaling/join", {
      method: "DELETE",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room, role }),
    });
  });

  return {
    send: (message) => fetch("/api/signaling/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room, role, message }),
    }),
  };
}