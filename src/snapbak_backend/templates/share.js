(() => {
  const media = document.querySelector("video, audio");
  let refreshed = 0;
  media.addEventListener("error", async () => {
    // The signed URL lasts an hour: past it, a seek fails. Sign a fresh one, at most once a minute, and resume.
    if (Date.now() - refreshed < 60000) return;
    refreshed = Date.now();
    const at = media.currentTime;
    const playing = !media.paused;
    const answer = await fetch(location.pathname + "/src", { cache: "no-store" });
    if (!answer.ok) return;
    const fresh = await answer.json();
    media.addEventListener(
      "loadedmetadata",
      () => {
        media.currentTime = at;
        if (playing) media.play();
      },
      { once: true },
    );
    media.src = fresh.video;
  });
  // The audio page's own player: play and pause, and the lit level, the position and the readout follow the element.
  const play = document.querySelector("button.play");
  if (play) {
    const seek = document.querySelector("input[type=range]");
    const level = document.querySelector(".level");
    const now = document.querySelector("[data-now]");
    const total = document.querySelector("[data-total]");
    const two = (number) => String(number).padStart(2, "0");
    const stamp = (seconds) => {
      const whole = Math.floor(seconds);
      const hours = Math.floor(whole / 3600);
      const minutes = Math.floor(whole / 60) % 60;
      return hours ? `${hours}:${two(minutes)}:${two(whole % 60)}` : `${two(minutes)}:${two(whole % 60)}`;
    };
    const show = () => {
      const length = Number.isFinite(media.duration) ? media.duration : Number(seek.max) / 1000;
      seek.max = String(Math.round(length * 1000));
      seek.value = String(Math.round(media.currentTime * 1000));
      level.style.width = `${length ? (100 * media.currentTime) / length : 0}%`;
      now.replaceChildren(stamp(media.currentTime));
      total.replaceChildren(stamp(length));
    };
    play.addEventListener("click", () => (media.paused ? media.play() : media.pause()));
    seek.addEventListener("input", () => {
      media.currentTime = Number(seek.value) / 1000;
    });
    media.addEventListener("timeupdate", show);
    media.addEventListener("loadedmetadata", show);
    media.addEventListener("play", () => {
      play.dataset.playing = "";
      play.setAttribute("aria-label", "Pause");
    });
    media.addEventListener("pause", () => {
      delete play.dataset.playing;
      play.setAttribute("aria-label", "Play");
    });
  }
  // A meeting's timestamps (`#t=<seconds>`) seek the player.
  document.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#t="]');
    if (!link) return;
    event.preventDefault();
    media.currentTime = Number(link.getAttribute("href").slice(3));
    media.play();
  });
})();
