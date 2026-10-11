"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const runningStates = new Set(["queued", "starting", "running", "cancelling"]);
  const finishedStates = new Set(["succeeded", "completed"]);
  const statusLabels = {
    created: "待制作", draft: "待制作", idle: "待制作", ready: "已保存", queued: "等待制作",
    starting: "正在启动", running: "制作中", cancelling: "正在停止",
    succeeded: "已完成", completed: "已完成", failed: "制作未完成", cancelled: "已停止", interrupted: "制作已中断",
  };
  const state = {
    token: "", engine: null, projects: [], selected: null, files: [],
    active: false, busy: false, slide: 0, tab: "preview", poll: null,
    loadVersion: 0, toastTimer: null, engineLoading: false,
  };

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.setAttribute("aria-hidden", "true");
    svg.append(use);
    return svg;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function projectUrl(id, suffix = "") {
    return `/api/projects/${encodeURIComponent(id)}${suffix}`;
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") headers.set("X-Web-GUI-Token", state.token);
    let response;
    try {
      response = await fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    } catch (error) {
      throw new Error("无法连接本地服务。请确认一键启动窗口仍在运行，然后刷新页面。");
    }
    let data;
    try { data = await response.json(); }
    catch (error) { throw new Error("服务返回了无法读取的内容，请查看一键启动窗口。"); }
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}），请重试。`);
    return data;
  }

  function showError(id, message = "") {
    $(id).textContent = message;
    $(id).hidden = !message;
  }

  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }

  function formatSize(size) {
    if (!Number.isFinite(Number(size))) return "";
    const bytes = Number(size);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatDate(value, withTime = false) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
    });
  }

  function statusOf(project) { return project?.job?.state || project?.status || "draft"; }
  function isRunning(project) { return runningStates.has(statusOf(project)); }
  function canGenerate() { return !!(state.engine?.available && state.engine?.authenticated); }

  function updateGenerateButton() {
    $("generate").disabled = state.busy || !canGenerate() || state.active;
    $("generate").classList.toggle("busy", state.busy);
    $("generate").querySelector("span").textContent = state.busy ? "正在提交…" : state.active ? "已有项目正在制作" : "开始生成";
    $("generate").title = !canGenerate() ? "请先完成 Agnes API 配置" : state.active ? "当前制作结束后可新建演示" : "";
    $("new-project").disabled = state.busy;
    $("retry-job").disabled = state.busy || state.active || !canGenerate();
  }

  async function checkEngine() {
    if (state.engineLoading) return;
    state.engineLoading = true;
    $("engine-label").textContent = "检查引擎中";
    $("engine-indicator").querySelector(".status-dot").className = "status-dot checking";
    $("refresh-engine").disabled = true;
    $("engine-indicator").disabled = true;
    try {
      const result = await api("/api/status");
      state.token = result.token || "";
      state.engine = result.engine;
      state.active = !!result.active;
      const ready = canGenerate();
      $("engine-label").textContent = ready ? "Agnes 已配置" : "Agnes 待配置";
      $("engine-indicator").querySelector(".status-dot").className = `status-dot ${ready ? "ready" : "unavailable"}`;
      $("engine-notice").hidden = ready;
      $("engine-notice-title").textContent = "完成 Agnes API 配置后，即可开始制作";
      $("engine-notice-detail").textContent = result.engine?.detail || "请检查本机私有模型配置文件或环境变量。";
      $("engine-model").textContent = `模型：${result.engine?.model || "agnes-2.5-flash"}`;
      showError("connection-error");
    } catch (error) {
      state.engine = null;
      $("engine-label").textContent = "服务未连接";
      $("engine-indicator").querySelector(".status-dot").className = "status-dot unavailable";
      showError("connection-error", error.message);
    } finally {
      state.engineLoading = false;
      $("refresh-engine").disabled = false;
      $("engine-indicator").disabled = false;
      updateGenerateButton();
    }
  }

  function renderProjectList() {
    const fragment = document.createDocumentFragment();
    for (const project of state.projects) {
      const item = element("button", "project-item");
      item.type = "button";
      item.classList.toggle("active", project.id === state.selected?.id);
      if (project.id === state.selected?.id) item.setAttribute("aria-current", "page");
      item.title = project.title;
      item.append(icon("slides"));
      const text = element("span");
      text.append(element("strong", "", project.title || "未命名演示"));
      const meta = element("small");
      meta.append(element("span", `status-dot ${statusOf(project)}`));
      meta.append(document.createTextNode(`${statusLabels[statusOf(project)] || "已保存"} · ${formatDate(project.created_at)}`));
      text.append(meta);
      item.append(text);
      item.addEventListener("click", () => selectProject(project.id));
      fragment.append(item);
    }
    $("project-list").replaceChildren(fragment);
    $("project-count").textContent = state.projects.length;
    $("empty-projects").hidden = state.projects.length > 0;
  }

  async function loadProjects() {
    const result = await api("/api/projects");
    state.projects = result.projects || [];
    state.active = state.projects.some(isRunning);
    renderProjectList();
    updateGenerateButton();
    schedulePoll();
  }

  function upsertProject(project) {
    const index = state.projects.findIndex((item) => item.id === project.id);
    if (index >= 0) state.projects[index] = project;
    else state.projects.unshift(project);
    state.active = state.projects.some(isRunning);
    renderProjectList();
    updateGenerateButton();
  }

  function newProject() {
    state.loadVersion += 1;
    state.selected = null;
    $("create-view").hidden = false;
    $("project-view").hidden = true;
    $("breadcrumb-current").textContent = "新建演示";
    renderProjectList();
    showError("form-error");
    $("topic").focus();
    schedulePoll();
  }

  async function selectProject(id) {
    if (state.busy) return;
    const version = ++state.loadVersion;
    try {
      const project = await api(projectUrl(id));
      if (version !== state.loadVersion) return;
      state.slide = 0;
      showProject(project);
      setTab(isRunning(project) && !project.slides?.length ? project.outline ? "outline" : "log" : "preview");
      showError("connection-error");
    } catch (error) { showError("connection-error", error.message); }
  }

  function showProject(project) {
    state.selected = project;
    $("create-view").hidden = true;
    $("project-view").hidden = false;
    $("breadcrumb-current").textContent = project.title || "演示项目";
    $("project-title").textContent = project.title || "未命名演示";
    const brief = project.brief || {};
    const editableHere = brief.mode === "quick";
    const planned = project.planned_pages || project.job?.planned_pages;
    const pageLabel = planned ? `已规划 ${planned} 页` : brief.page_count_mode === "range" ? `规划范围 ${brief.min_pages}–${brief.max_pages} 页` : brief.slides ? `指定 ${brief.slides} 页` : brief.page_count_mode === "auto" ? "自动规划页数" : "";
    const meta = [formatDate(project.created_at, true), pageLabel, brief.format === "ppt43" ? "4:3 标准画面" : brief.format ? "16:9 宽屏画面" : ""].filter(Boolean);
    $("project-meta").textContent = meta.join("  ·  ");
    const status = statusOf(project);
    const running = runningStates.has(status);
    const complete = finishedStates.has(status) || (!project.job && !!project.exports?.length);
    $("project-status").className = `status-badge ${status}`;
    $("project-status").textContent = statusLabels[status] || "已保存";
    $("job-banner").classList.toggle("complete", complete);
    $("job-banner-icon").replaceChildren(icon(complete ? "check" : running ? "refresh" : "slides"));
    $("job-banner-icon").querySelector("svg").classList.toggle("spinning", running);
    const titles = {
      queued: "你的演示已进入制作队列", starting: "正在启动制作", running: "AI 正在制作你的演示",
      cancelling: "正在停止制作", failed: "制作暂时未能完成", cancelled: "制作已停止", interrupted: "制作已中断",
    };
    $("job-title").textContent = complete ? "演示已完成，可以下载了" : !editableHere && !running ? "浏览已有演示项目" : titles[status] || "项目已保存，准备好即可开始";
    $("job-description").textContent = complete ? "逐页检查内容与排版，在「文件与下载」获取演示文件。" : running ? "页面与日志会自动更新，你也可以继续浏览已有项目。" : !editableHere ? "此项目可在这里预览和下载；如需继续制作，请在原工作流程中打开。" : status === "failed" ? "查看制作进展了解原因，处理后可重新生成。" : ["cancelled", "interrupted"].includes(status) ? "已有页面和资料仍保存在项目中，可重新开始制作。" : "项目要求与资料均已保存。";
    if (running && project.job?.stage_label) {
      $("job-title").textContent = project.job.stage_label;
      const current = Number(project.job.current);
      const total = Number(project.job.total);
      const detail = project.job.message || "内容会自动更新。";
      if (Number.isFinite(current) && total > 0) {
        $("job-description").textContent = `当前进度 ${current} / ${total} · ${detail}`;
      } else {
        $("job-description").textContent = detail;
      }
    }
    $("cancel-job").hidden = !running;
    $("cancel-job").disabled = status === "cancelling" || state.busy;
    $("retry-job").hidden = !editableHere || running || complete;
    $("retry-job").lastChild.textContent = ["failed", "cancelled", "interrupted"].includes(status) ? "重新生成" : "开始生成";
    showError("project-error", project.job?.error || "");
    const log = $("job-log");
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    const logText = Array.isArray(project.job?.log) ? project.job.log.join("\n") : project.job?.log;
    if (log.textContent !== (logText || "项目开始制作后，实际进展会显示在这里。")) {
      log.textContent = logText || "项目开始制作后，实际进展会显示在这里。";
      if (nearBottom) log.scrollTop = log.scrollHeight;
    }
    $("log-updated").textContent = project.job?.updated_at ? `更新于 ${formatDate(project.job.updated_at, true)}` : "";
    $("log-live-dot").className = `status-dot ${status}`;
    renderSlides(project);
    renderOutline(project);
    renderFiles(project);
    upsertProject(project);
    schedulePoll();
  }

  function safeFileUrl(value) {
    try {
      const url = new URL(value, window.location.origin);
      return url.origin === window.location.origin && url.pathname.startsWith("/api/projects/") ? url.href : null;
    } catch (error) { return null; }
  }

  function renderSlides(project) {
    const slides = project.slides || [];
    $("slide-count").textContent = slides.length;
    $("preview-empty").hidden = slides.length > 0;
    $("slide-viewer").hidden = !slides.length;
    if (!slides.length) {
      $("slide-frame").removeAttribute("src");
      return;
    }
    state.slide = Math.max(0, Math.min(state.slide, slides.length - 1));
    const slide = slides[state.slide];
    const url = safeFileUrl(slide.url);
    if (url && $("slide-frame").src !== url) $("slide-frame").src = url;
    if (!url) $("slide-frame").removeAttribute("src");
    $("slide-frame").title = `第 ${state.slide + 1} 页：${slide.name}`;
    $("slide-frame").style.aspectRatio = project.brief?.format === "ppt43" ? "4 / 3" : "16 / 9";
    $("slide-position").textContent = `${state.slide + 1} / ${slides.length}`;
    $("slide-name").textContent = slide.name;
    $("previous-slide").disabled = state.slide === 0;
    $("next-slide").disabled = state.slide >= slides.length - 1;
    const fragment = document.createDocumentFragment();
    slides.forEach((item, index) => {
      const button = element("button", `slide-choice${index === state.slide ? " active" : ""}`, String(index + 1).padStart(2, "0"));
      button.type = "button";
      button.title = item.name;
      button.setAttribute("aria-label", `查看第 ${index + 1} 页`);
      button.setAttribute("aria-pressed", String(index === state.slide));
      button.addEventListener("click", () => { state.slide = index; renderSlides(state.selected); });
      fragment.append(button);
    });
    $("slide-thumbnails").replaceChildren(fragment);
    const note = pageNotes(project, state.slide);
    $("slide-notes").hidden = !note;
    $("slide-notes-content").textContent = note;
  }

  function pageNotes(project, index) {
    const name = project.slides?.[index]?.name?.replace(/\.svg$/i, ".md");
    const finalNotes = (project.notes || []).filter((note) => note.name.toLowerCase() !== "total.md");
    const note = finalNotes.find((item) => item.name === name);
    return note?.content || project.outline?.slides?.[index]?.notes || "";
  }

  function renderOutline(project) {
    const outline = project.outline;
    const slides = Array.isArray(outline?.slides) ? outline.slides : [];
    $("outline-empty").hidden = slides.length > 0;
    $("outline-content").hidden = !slides.length;
    if (!slides.length) return;
    $("outline-title").textContent = outline.title || "演示内容规划";
    $("outline-count").textContent = `${slides.length} 页`;
    $("outline-reason").textContent = outline.page_count_reason || "根据内容重点安排页面结构。";
    const expanded = new Set(Array.from($("outline-list").querySelectorAll("details[open]")).map((item) => item.dataset.page));
    $("outline-list").replaceChildren(...slides.map((slide, index) => {
      const item = element("li", "outline-page");
      const heading = element("div", "outline-page-heading");
      heading.append(element("span", "section-number", String(index + 1).padStart(2, "0")), element("h3", "", slide.title || `第 ${index + 1} 页`));
      item.append(heading);
      if (slide.description) item.append(element("p", "outline-description", slide.description));
      const points = element("ul", "outline-points");
      for (const point of Array.isArray(slide.content_points) ? slide.content_points : []) points.append(element("li", "", point));
      item.append(points);
      const note = pageNotes(project, index);
      if (note) {
        const details = element("details", "outline-notes");
        details.dataset.page = String(index);
        details.open = expanded.has(String(index));
        details.append(element("summary", "", "演讲备注"), element("p", "", note));
        item.append(details);
      }
      return item;
    }));
  }

  function renderFiles(project) {
    const exports = [...(project.exports || [])];
    if (project.outline_url) exports.push({ name: "outline.json", url: project.outline_url });
    for (const note of project.notes || []) exports.push(note);
    $("export-count").textContent = exports.length;
    $("export-empty").hidden = exports.length > 0;
    const fragment = document.createDocumentFragment();
    exports.forEach((file) => {
      const row = element("div", "export-file");
      row.append(element("span", "file-tile", (file.name.split(".").pop() || "FILE").toUpperCase()));
      const detail = element("div");
      detail.append(element("strong", "", file.name), element("small", "", formatSize(file.size)));
      row.append(detail);
      const url = safeFileUrl(file.url);
      if (url) {
        const download = element("a", "", "下载");
        download.href = url;
        download.download = file.name;
        download.prepend(icon("download"));
        row.append(download);
      }
      fragment.append(row);
    });
    $("export-list").replaceChildren(fragment);
    const sources = project.sources || [];
    $("source-empty").hidden = sources.length > 0;
    $("source-list").replaceChildren(...sources.map((source) => {
      const item = element("li");
      item.append(icon("file"), element("span", "", source.name), element("small", "", formatSize(source.size)));
      return item;
    }));
    const brief = project.brief || {};
    $("project-brief").textContent = [brief.topic, brief.style ? `设计偏好：${brief.style}` : ""].filter(Boolean).join("\n\n");
    $("project-path").textContent = project.path || "";
  }

  function setTab(tab, focus = false) {
    state.tab = tab;
    document.querySelectorAll(".result-tab").forEach((button) => {
      const selected = button.dataset.tab === tab;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(button.getAttribute("aria-controls")).hidden = !selected;
      if (focus && selected) button.focus();
    });
  }

  function schedulePoll() {
    clearTimeout(state.poll);
    if (!state.active && !isRunning(state.selected)) return;
    state.poll = setTimeout(poll, 2000);
  }

  async function poll() {
    try {
      const id = state.selected?.id;
      const version = state.loadVersion;
      const project = id ? await api(projectUrl(id)) : null;
      if (project && state.selected?.id === id && version === state.loadVersion) {
        const previous = statusOf(state.selected);
        showProject(project);
        if (runningStates.has(previous) && finishedStates.has(statusOf(project))) {
          toast("演示制作完成，可以预览和下载了。");
          if (state.tab === "log") setTab("preview");
        }
      }
      await loadProjects();
      showError("connection-error");
    } catch (error) { showError("connection-error", error.message); }
    schedulePoll();
  }

  function addFiles(files) {
    const allowed = new Set($("files").accept.split(","));
    const rejected = [];
    for (const file of files) {
      if (state.files.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) continue;
      const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      if (!allowed.has(extension)) { rejected.push(`${file.name}：不支持此文件类型`); continue; }
      if (state.files.length >= 20) { rejected.push("最多上传 20 个文件"); break; }
      if (state.files.reduce((sum, item) => sum + item.size, 0) + file.size > 100 * 1024 * 1024) {
        rejected.push(`${file.name}：文件总大小不能超过 100 MB`);
        continue;
      }
      state.files.push(file);
    }
    showError("form-error", rejected.join("；"));
    renderUploads();
  }

  function renderUploads() {
    $("upload-list").replaceChildren(...state.files.map((file, index) => {
      const item = element("li");
      const name = element("span", "", file.name);
      name.title = file.name;
      const remove = element("button", "icon-button");
      remove.type = "button";
      remove.setAttribute("aria-label", `移除 ${file.name}`);
      remove.append(icon("close"));
      remove.addEventListener("click", () => { state.files.splice(index, 1); renderUploads(); });
      item.append(icon("file"), name, element("small", "", formatSize(file.size)), remove);
      return item;
    }));
  }

  async function generateProject(event) {
    event.preventDefault();
    if (state.busy || !canGenerate() || state.active) return;
    if (!$("create-form").reportValidity()) return;
    if (!$("topic").value.trim() && !state.files.length) {
      showError("form-error", "请填写演示主题，或上传至少一份参考资料。");
      $("topic").focus();
      return;
    }
    if ($("page-count-mode").value === "range" && Number($("min-pages").value) > Number($("max-pages").value)) {
      showError("form-error", "最少页数不能大于最多页数。");
      $("min-pages").focus();
      return;
    }
    state.busy = true;
    updateGenerateButton();
    showError("form-error");
    const data = new FormData();
    ["topic", "format", "style"].forEach((name) => data.append(name, $(name).value.trim()));
    data.append("page_count_mode", $("page-count-mode").value);
    data.append("analysis_depth", $("analysis-depth").value);
    if ($("page-count-mode").value === "fixed") data.append("slides", $("slides").value);
    if ($("page-count-mode").value === "range") {
      data.append("min_pages", $("min-pages").value);
      data.append("max_pages", $("max-pages").value);
    }
    state.files.forEach((file) => data.append("files", file, file.name));
    try {
      const project = await api("/api/projects", { method: "POST", body: data });
      state.loadVersion += 1;
      state.slide = 0;
      showProject(project);
      setTab("log");
      $("create-form").reset();
      updatePageInputs();
      $("topic-length").textContent = "0";
      state.files = [];
      renderUploads();
      await startGeneration(project.id);
    } catch (error) {
      showError(state.selected ? "project-error" : "form-error", error.message);
    } finally {
      state.busy = false;
      updateGenerateButton();
      if (state.selected) $("cancel-job").disabled = statusOf(state.selected) === "cancelling";
    }
  }

  async function startGeneration(id) {
    await api(projectUrl(id, "/generate"), { method: "POST" });
    state.active = true;
    const project = await api(projectUrl(id));
    if (state.selected?.id === id) showProject(project);
    await loadProjects();
    schedulePoll();
  }

  async function retryGeneration() {
    if (state.busy || state.selected?.brief?.mode !== "quick" || state.active || !canGenerate()) return;
    state.busy = true;
    updateGenerateButton();
    showError("project-error");
    try {
      setTab("log");
      await startGeneration(state.selected.id);
    } catch (error) { showError("project-error", error.message); }
    finally { state.busy = false; updateGenerateButton(); $("cancel-job").disabled = false; }
  }

  async function cancelGeneration() {
    if (!state.selected || state.busy) return;
    $("cancel-job").disabled = true;
    try {
      const id = state.selected.id;
      await api(projectUrl(id, "/cancel"), { method: "POST" });
      const project = await api(projectUrl(id));
      if (state.selected?.id === id) showProject(project);
      await loadProjects();
      toast("已请求停止制作，已有文件会保留。");
    } catch (error) {
      showError("project-error", error.message);
      $("cancel-job").disabled = false;
    }
  }

  function updatePageInputs() {
    const mode = $("page-count-mode").value;
    $("fixed-pages").hidden = mode !== "fixed";
    $("range-pages").hidden = mode !== "range";
    $("slides").disabled = mode !== "fixed";
    $("slides").required = mode === "fixed";
    for (const id of ["min-pages", "max-pages"]) {
      $(id).disabled = mode !== "range";
      $(id).required = mode === "range";
    }
    $("page-count-help").textContent = mode === "auto" ? "AI 根据内容在 5–30 页内自动规划结构与页数。" : mode === "fixed" ? "指定 1–40 页，AI 将内容组织为对应页数。" : "设定 1–40 页内的范围，AI 在范围内安排合适页数。";
  }

  $("new-project").addEventListener("click", newProject);
  $("refresh-engine").addEventListener("click", checkEngine);
  $("engine-indicator").addEventListener("click", checkEngine);
  $("topic").addEventListener("input", () => { $("topic-length").textContent = $("topic").value.length; });
  $("page-count-mode").addEventListener("change", updatePageInputs);
  $("files").addEventListener("change", () => { addFiles($("files").files); $("files").value = ""; });
  $("drop-zone").addEventListener("dragover", (event) => { event.preventDefault(); $("drop-zone").classList.add("dragging"); });
  $("drop-zone").addEventListener("dragleave", () => $("drop-zone").classList.remove("dragging"));
  $("drop-zone").addEventListener("drop", (event) => {
    event.preventDefault();
    $("drop-zone").classList.remove("dragging");
    addFiles(event.dataTransfer.files);
  });
  $("create-form").addEventListener("submit", generateProject);
  $("cancel-job").addEventListener("click", cancelGeneration);
  $("retry-job").addEventListener("click", retryGeneration);
  $("see-progress").addEventListener("click", () => setTab("log", true));
  $("previous-slide").addEventListener("click", () => { state.slide -= 1; renderSlides(state.selected); });
  $("next-slide").addEventListener("click", () => { state.slide += 1; renderSlides(state.selected); });
  document.querySelectorAll(".result-tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      const tabs = ["outline", "preview", "log", "files"];
      const current = tabs.indexOf(button.dataset.tab);
      let index;
      if (event.key === "ArrowRight") index = (current + 1) % tabs.length;
      else if (event.key === "ArrowLeft") index = (current + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = tabs.length - 1;
      else return;
      event.preventDefault();
      setTab(tabs[index], true);
    });
  });
  updatePageInputs();
  updateGenerateButton();
  Promise.allSettled([checkEngine(), loadProjects()]).then((results) => {
    for (const result of results) {
      if (result.status === "rejected") showError("connection-error", result.reason.message);
    }
  });
})();
