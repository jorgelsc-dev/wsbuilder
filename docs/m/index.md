# Metricas en Vivo

Esta pagina existe como panel oculto del sitio. No aparece en la navegacion principal, pero esta disponible en `/m/`.

Sirve como un monitor rapido para evaluar si el proyecto esta ganando traccion real.

## Que muestra

<div class="cards">
<div class="card"><strong>Stars</strong>Señal rapida de interes publico.</div>
<div class="card"><strong>Forks</strong>Uso practico y posibles derivaciones.</div>
<div class="card"><strong>Issues</strong>Actividad y friccion del proyecto.</div>
<div class="card"><strong>PRs</strong>Participacion externa y ritmo de colaboracion.</div>
</div>

<div class="cards">
<div class="card"><strong>Commits</strong>Movimiento reciente en la rama principal.</div>
<div class="card"><strong>Contribuidores</strong>Señal de comunidad real.</div>
<div class="card"><strong>Release</strong>Estado de publicacion o ultima version visible.</div>
<div class="card"><strong>Señal</strong>Lectura sintetica del estado del proyecto.</div>
</div>

## Dashboard

<div class="cards">
<div class="card"><strong>Repo</strong><span id="repo-name">wsbuilder</span></div>
<div class="card"><strong>Stars</strong><span id="repo-stars">-</span></div>
<div class="card"><strong>Forks</strong><span id="repo-forks">-</span></div>
<div class="card"><strong>Watchers</strong><span id="repo-watchers">-</span></div>
</div>

<div class="cards">
<div class="card"><strong>Open issues</strong><span id="repo-issues">-</span></div>
<div class="card"><strong>Open PRs</strong><span id="repo-prs">-</span></div>
<div class="card"><strong>Contributors</strong><span id="repo-contribs">-</span></div>
<div class="card"><strong>Last commit</strong><span id="repo-commit">-</span></div>
</div>

<div class="card" style="margin-top: 1rem;">
<strong>Lectura</strong>
<p id="repo-signal">Cargando senales publicas...</p>
</div>

## Como funciona

La pagina consulta APIs publicas de GitHub con `fetch()` y refresca los indicadores cada minuto. No usa secretos, por lo que solo muestra datos publicos visibles desde el navegador.

## Limitaciones

- El trafico interno del repo no es publico y no se puede leer de forma anonima desde el navegador.
- Los datos de GitHub tienen limites de tasa.
- Esta pagina mide senales publicas, no legitimidad absoluta.

## Lectura rapida

- Muchas estrellas y forks con commits recientes: buena senal.
- Issues abiertas creciendo mas rapido que la actividad de mantenimiento: senal de friccion.
- PRs externas y contributors activos: senal de comunidad real.

<script>
(async () => {
  const owner = "jorgelsc-dev";
  const repo = "wsbuilder";
  const repoUrl = `https://api.github.com/repos/${owner}/${repo}`;
  const contributorsUrl = `https://api.github.com/repos/${owner}/${repo}/contributors?per_page=100&anon=1`;
  const pullsUrl = `https://api.github.com/repos/${owner}/${repo}/pulls?state=open&per_page=100`;
  const commitsUrl = `https://api.github.com/repos/${owner}/${repo}/commits?per_page=1`;

  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  const signal = (text) => set("repo-signal", text);

  try {
    const [repoRes, contributorsRes, pullsRes, commitsRes] = await Promise.all([
      fetch(repoUrl, { cache: "no-store" }),
      fetch(contributorsUrl, { cache: "no-store" }),
      fetch(pullsUrl, { cache: "no-store" }),
      fetch(commitsUrl, { cache: "no-store" }),
    ]);

    if (!repoRes.ok) throw new Error(`repo:${repoRes.status}`);

    const repoData = await repoRes.json();
    const contributorsData = contributorsRes.ok ? await contributorsRes.json() : [];
    const pullsData = pullsRes.ok ? await pullsRes.json() : [];
    const commitsData = commitsRes.ok ? await commitsRes.json() : [];

    set("repo-name", `${owner}/${repo}`);
    set("repo-stars", repoData.stargazers_count ?? "-");
    set("repo-forks", repoData.forks_count ?? "-");
    set("repo-watchers", repoData.subscribers_count ?? repoData.watchers_count ?? "-");
    set("repo-issues", repoData.open_issues_count ?? "-");
    set("repo-prs", Array.isArray(pullsData) ? pullsData.length : "-");
    set("repo-contribs", Array.isArray(contributorsData) ? contributorsData.length : "-");

    const commit = Array.isArray(commitsData) && commitsData[0] ? commitsData[0] : null;
    if (commit && commit.commit) {
      const sha = commit.sha ? commit.sha.slice(0, 7) : "-";
      const when = commit.commit.committer && commit.commit.committer.date ? new Date(commit.commit.committer.date).toLocaleString() : "unknown";
      set("repo-commit", `${sha} @ ${when}`);
    } else {
      set("repo-commit", "unknown");
    }

    const stars = repoData.stargazers_count ?? 0;
    const forks = repoData.forks_count ?? 0;
    const contributors = Array.isArray(contributorsData) ? contributorsData.length : 0;
    const openIssues = repoData.open_issues_count ?? 0;
    const openPrs = Array.isArray(pullsData) ? pullsData.length : 0;
    const score = (stars * 2) + (forks * 3) + (contributors * 4) + Math.max(0, 20 - openIssues) + Math.max(0, 20 - openPrs);

    if (score >= 80) {
      signal("Alta traccion publica. Hay interes, derivaciones y actividad sostenida.");
    } else if (score >= 40) {
      signal("Senal intermedia. Hay interes real, pero la comunidad aun puede crecer.");
    } else {
      signal("Senal temprana. Hay presencia publica, pero hace falta mas uso, forks o contribucion externa.");
    }
  } catch (err) {
    signal(`No se pudo cargar el panel en este momento: ${err.message}`);
  }

  setInterval(() => {
    window.location.reload();
  }, 60000);
})();
</script>
