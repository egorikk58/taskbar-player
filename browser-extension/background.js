// Мост между вкладками Яндекс Музыки и Taskbar Player: состояние плеера — туда,
// нажатия кнопок мини-плеера — обратно. Только на этот компьютер, 127.0.0.1.

const SERVER = "ws://127.0.0.1:47821";
const latest = new Map(); // вкладка → последнее состояние (отправим заново после переподключения)
let ws = null;
let lastTry = 0;

function connect() {
  if (ws || Date.now() - lastTry < 3000) return; // плеер не запущен — не долбимся каждую секунду
  lastTry = Date.now();
  const sock = (ws = new WebSocket(SERVER));
  sock.onopen = () => latest.forEach((msg) => sock.send(JSON.stringify(msg)));
  sock.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "command" && latest.has(msg.tab)) {
      chrome.tabs.sendMessage(msg.tab, msg).catch(() => {});
    }
  };
  sock.onclose = () => {
    if (ws === sock) ws = null;
  };
}

function send(msg) {
  if (msg.type === "gone") latest.delete(msg.tab);
  else latest.set(msg.tab, msg);
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  else connect();
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (sender.tab) send({ ...msg, tab: sender.tab.id });
});
chrome.tabs.onRemoved.addListener((tab) => send({ type: "gone", tab }));

// Пока сокет открыт, браузер не усыпляет service worker, если раз в 30 с что-то отправлять
setInterval(() => {
  if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
}, 20000);
