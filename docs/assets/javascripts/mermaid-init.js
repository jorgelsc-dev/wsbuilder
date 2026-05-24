(() => {
  const bootstrap = () => {
    if (typeof mermaid === "undefined") {
      return false;
    }

    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "neutral",
      flowchart: {
        useMaxWidth: true,
        htmlLabels: true,
        curve: "basis",
      },
      sequence: {
        useMaxWidth: true,
        showSequenceNumbers: false,
      },
    });

    const blocks = document.querySelectorAll("pre > code.language-mermaid, pre > code.mermaid");
    blocks.forEach((code, index) => {
      const pre = code.parentElement;
      if (!pre || pre.dataset.mermaidRendered === "true") {
        return;
      }

      const diagram = document.createElement("div");
      diagram.className = "mermaid";
      diagram.textContent = code.textContent;

      pre.replaceWith(diagram);
      diagram.setAttribute("data-mermaid-id", `diagram-${index}`);
    });

    mermaid.run({ querySelector: ".mermaid" }).catch((error) => {
      console.error("Mermaid render error:", error);
    });
    return true;
  };

  const start = () => {
    if (bootstrap()) {
      return;
    }
    window.setTimeout(start, 60);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
