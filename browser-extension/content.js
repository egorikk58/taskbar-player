// Taskbar Player: передаёт мини-плееру, что играет в Яндекс Музыке.
// Расширение видит страницу всегда — даже в фоновой вкладке, где Windows её уже не видит.
// Название, исполнителя и обложку сайт сам кладёт в navigator.mediaSession,
// время и кнопки берём из блока «Плеер» на странице.

const BUTTONS = {
  prev: ["Предыдущая песня"],
  toggle: ["Пауза", "Воспроизведение"],
  next: ["Следующая песня"],
};

function playerRoot() {
  return (
    document.getElementById("player-region")?.closest("section") ??
    document.querySelector('[aria-label="Следующая песня"]')?.closest("section")
  );
}

function linkId(root, kind) {
  const href = root?.querySelector(`a[href*="/${kind}/"]`)?.getAttribute("href") ?? "";
  return href.match(new RegExp(`/${kind}/(\\d+)`))?.[1] ?? null;
}

function snapshot() {
  const meta = navigator.mediaSession.metadata;
  if (!meta?.title) return { type: "gone" };
  const root = playerRoot();
  const slider = root?.querySelector('input[aria-label="Управление таймкодом"]');
  const timecode = slider?.getAttribute("aria-valuetext")?.trim() || null;
  // ползунок в секундах; пока трек не загружен, подписи нет, а max — заглушка
  const max = timecode ? Number(slider.max) || 0 : 0;
  const state = navigator.mediaSession.playbackState;
  return {
    type: "state",
    title: meta.title,
    artist: meta.artist,
    artwork: meta.artwork.at(-1)?.src ?? null, // самая большая
    trackId: linkId(root, "track"),
    albumId: linkId(root, "album"),
    playing: state === "none" ? !!root?.querySelector('[aria-label="Пауза"]') : state === "playing",
    pos: max > 0 ? Number(slider.value) : null,
    dur: max > 0 ? max : null,
    timecode,
  };
}

let lastKey = "";
let lastSent = 0;

function report() {
  const msg = snapshot();
  // время меняется каждую секунду — из-за него одного шлём не чаще раза в секунду
  const key = JSON.stringify({ ...msg, pos: null, timecode: null });
  if (key === lastKey && Date.now() - lastSent < 1000) return;
  lastKey = key;
  lastSent = Date.now();
  try {
    chrome.runtime.sendMessage(msg).catch(() => {});
  } catch {
    // расширение обновили или выключили — эта копия скрипта больше не нужна
    observer.disconnect();
    clearInterval(timer);
  }
}

const observer = new MutationObserver(report);
observer.observe(document.documentElement, {
  subtree: true,
  childList: true,
  characterData: true,
  attributes: true,
  attributeFilter: ["aria-label", "aria-valuetext"],
});
const timer = setInterval(report, 1000);

addEventListener("pagehide", () => {
  try {
    chrome.runtime.sendMessage({ type: "gone" }).catch(() => {});
  } catch {}
});

// нажатия кнопок мини-плеера — только внутри блока «Плеер»,
// в списках треков на странице тоже есть кнопки «Воспроизведение»
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type !== "command") return;
  const root = playerRoot();
  const button = (BUTTONS[msg.command] ?? [])
    .map((label) => root?.querySelector(`[aria-label="${label}"]`))
    .find(Boolean);
  button?.click();
});
