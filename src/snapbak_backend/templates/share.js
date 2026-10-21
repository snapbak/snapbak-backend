(() => {
  const video = document.querySelector("video");
  let refreshed = 0;
  video.addEventListener("error", async () => {
    // The signed URL lasts an hour: past it, a seek fails. Sign a fresh one, at most once a minute, and resume.
    if (Date.now() - refreshed < 60000) return;
    refreshed = Date.now();
    const at = video.currentTime;
    const playing = !video.paused;
    const answer = await fetch(location.pathname + "/src", { cache: "no-store" });
    if (!answer.ok) return;
    const fresh = await answer.json();
    video.addEventListener(
      "loadedmetadata",
      () => {
        video.currentTime = at;
        if (playing) video.play();
      },
      { once: true },
    );
    video.src = fresh.video;
  });
})();
