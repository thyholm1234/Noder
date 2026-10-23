// Rehearsal mode: play one voice through an entire setlist, song after song.
(function () {
    'use strict';

    const root = document.getElementById('rehearsal');
    if (!root) return;

    const $ = (sel) => root.querySelector(sel);
    const norm = (s) => (s || '').toLowerCase().trim();
    const fmt = (t) => (isFinite(t) ? `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}` : '0:00');

    const songs = JSON.parse(root.dataset.playlist);
    const audio = $('[data-player-audio]');
    const voiceSel = $('[data-player-voice]');
    const rateSel = $('[data-player-rate]');
    const seek = $('[data-player-seek]');
    const loopBtn = $('[data-player-loop]');
    let queue = [];
    let index = -1;
    let loop = false;

    function pickTrack(song, voice) {
        if (!song.tracks.length) return null;
        const v = norm(voice);
        if (!v) return song.tracks[0];
        return song.tracks.find((t) => norm(t.part) === v)
            || song.tracks.find((t) => norm(t.part).startsWith(v) || norm(t.part).includes(v))
            || null;
    }

    function buildQueue() {
        const voice = voiceSel.value;
        queue = songs.map((s, i) => ({ song: s, number: i + 1, track: pickTrack(s, voice) })).filter((q) => q.track);
        const skipped = songs.length - queue.length;
        $('[data-player-sub]').textContent = queue.length
            ? `${queue.length} sang${queue.length !== 1 ? 'e' : ''} med ${voice || 'lyd'}${skipped ? ` · ${skipped} uden` : ''}`
            : `Ingen lydfiler for ${voice || 'denne stemme'}`;
    }

    function highlightRow() {
        const current = queue[index];
        document.querySelectorAll('[data-song-row]').forEach((row) => {
            row.classList.toggle('is-playing', !!current && +row.dataset.songRow === current.song.id);
        });
    }

    function load(i, autoplay = true) {
        if (!queue.length) return;
        index = (i + queue.length) % queue.length;
        const q = queue[index];
        audio.src = q.track.url;
        audio.playbackRate = parseFloat(rateSel.value);
        $('[data-player-index]').textContent = q.number;
        $('[data-player-title]').textContent = q.song.title;
        $('[data-player-sub]').textContent = [q.track.part, q.song.composer].filter(Boolean).join(' · ');
        highlightRow();
        if (autoplay) audio.play().catch(() => {});
        if ('mediaSession' in navigator) {
            navigator.mediaSession.metadata = new MediaMetadata({ title: q.song.title, artist: q.track.part, album: document.title });
        }
    }

    $('[data-player-toggle]').addEventListener('click', () => {
        if (index < 0) return load(0);
        audio.paused ? audio.play() : audio.pause();
    });
    $('[data-player-next]').addEventListener('click', () => load(index + 1));
    $('[data-player-prev]').addEventListener('click', () => (audio.currentTime > 3 ? (audio.currentTime = 0) : load(index - 1)));
    loopBtn.addEventListener('click', () => { loop = !loop; loopBtn.classList.toggle('active', loop); });

    audio.addEventListener('ended', () => {
        if (loop) { audio.currentTime = 0; audio.play(); }
        else if (index < queue.length - 1) load(index + 1);
        else root.classList.remove('is-playing');
    });
    audio.addEventListener('play', () => root.classList.add('is-playing'));
    audio.addEventListener('pause', () => root.classList.remove('is-playing'));
    audio.addEventListener('loadedmetadata', () => { $('[data-player-duration]').textContent = fmt(audio.duration); });
    audio.addEventListener('timeupdate', () => {
        $('[data-player-time]').textContent = fmt(audio.currentTime);
        if (audio.duration && !seek.matches(':active')) seek.value = Math.round((audio.currentTime / audio.duration) * 1000);
    });
    seek.addEventListener('input', () => { if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration; });

    try { rateSel.value = localStorage.getItem('noder.rate') || '1'; } catch (e) { /* ignore */ }
    if (!rateSel.value) rateSel.value = '1';
    rateSel.addEventListener('change', () => {
        audio.playbackRate = parseFloat(rateSel.value);
        try { localStorage.setItem('noder.rate', rateSel.value); } catch (e) { /* ignore */ }
    });

    voiceSel.addEventListener('change', () => {
        const wasPlaying = !audio.paused;
        const currentId = queue[index]?.song.id;
        buildQueue();
        const again = queue.findIndex((q) => q.song.id === currentId);
        if (again >= 0) {
            const t = audio.currentTime;
            load(again, wasPlaying);
            audio.addEventListener('loadedmetadata', () => { audio.currentTime = t; }, { once: true });
        } else {
            index = -1;
            audio.removeAttribute('src');
            highlightRow();
        }
    });

    if ('mediaSession' in navigator) {
        navigator.mediaSession.setActionHandler('nexttrack', () => load(index + 1));
        navigator.mediaSession.setActionHandler('previoustrack', () => load(index - 1));
    }

    window.noderActiveAudio = audio;
    buildQueue();
})();
