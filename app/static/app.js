// PayPilot's few lines of client-side glue. Everything else is server-rendered.
(function () {
  function toast(text) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.classList.remove("show"); }, 6000);
  }

  // Our error responses are HTML fragments with a friendly message: show them.
  document.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail.xhr;
    if (xhr.status >= 400) {
      var html = (xhr.getResponseHeader("Content-Type") || "").indexOf("text/html") === 0;
      if (html && xhr.responseText) {
        e.detail.shouldSwap = true;
        e.detail.isError = false;
      } else {
        toast("Something went wrong (" + xhr.status + "). Nothing was sent; try again.");
      }
    }
  });
  document.addEventListener("htmx:sendError", function () {
    toast("Can't reach PayPilot right now. Check your connection and try again.");
  });
  document.addEventListener("htmx:timeout", function () {
    toast("That took too long. Check the audit log before retrying.");
  });

  // Chat: Enter sends, Shift+Enter adds a line; suggestion chips fill the box.
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (t.matches && t.matches("textarea[data-enter-submits]") && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (t.value.trim()) htmx.trigger(t.form, "submit");
    }
  });
  document.addEventListener("click", function (e) {
    var chip = e.target.closest && e.target.closest("[data-suggest]");
    if (!chip) return;
    var box = document.getElementById("message");
    box.value = chip.getAttribute("data-suggest");
    box.focus();
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target.id === "messages") {
      e.detail.target.lastElementChild && e.detail.target.lastElementChild.scrollIntoView({ block: "end" });
    }
  });

  // Escalated approvals: Approve stays disabled until "I've checked this" is ticked.
  document.addEventListener("change", function (e) {
    if (e.target.name !== "acknowledge_anomaly") return;
    var btn = e.target.form.querySelector("button[data-needs-ack]");
    if (btn) btn.disabled = !e.target.checked;
  });
})();
