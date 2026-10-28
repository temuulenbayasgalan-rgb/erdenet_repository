/*
 * Public job map (Leaflet + OpenStreetMap tiles). Optional: if Leaflet or the tiles
 * fail to load, the server-rendered job list still works on its own.
 * Popup text is set with textContent, never innerHTML, so listing text can't inject HTML.
 */
(function () {
  "use strict";

  function showProblem(container, message) {
    var note = document.getElementById("map-problem");
    if (note) {
      note.textContent = message;
      note.classList.remove("d-none");
    }
    if (container) container.classList.add("map-failed");
  }

  function line(tag, text, className) {
    var el = document.createElement(tag);
    el.textContent = text;
    if (className) el.className = className;
    return el;
  }

  function popupFor(point) {
    var box = document.createElement("div");
    box.appendChild(line("div", point.title, "fw-semibold"));
    box.appendChild(line("div", point.date + " · " + point.time, "small"));
    box.appendChild(line("div", point.wage + " / өдөр · " + point.remaining + " хүн хэрэгтэй", "small"));
    box.appendChild(line("div", point.area, "small text-muted"));
    var link = line("a", "Дэлгэрэнгүй · хүсэлт илгээх", "btn btn-sm btn-primary mt-2 text-white");
    link.href = point.url;
    box.appendChild(link);
    return box;
  }

  function init() {
    var container = document.getElementById("job-map");
    if (!container) return;
    var dataEl = document.getElementById("job-map-data");
    var points = dataEl ? JSON.parse(dataEl.textContent) : [];

    if (typeof L === "undefined") {
      showProblem(container, "Газрын зураг ачаалагдсангүй. Доорх жагсаалтыг ашиглана уу.");
      return;
    }

    var cfg = container.dataset;
    var map = L.map(container, { scrollWheelZoom: false })
      .setView([parseFloat(cfg.centerLat), parseFloat(cfg.centerLng)], parseInt(cfg.zoom, 10));
    var tiles = L.tileLayer(cfg.tileUrl, { maxZoom: 18, attribution: cfg.attribution });
    var tileErrors = 0;
    tiles.on("tileerror", function () {
      tileErrors += 1;
      if (tileErrors === 3) {
        showProblem(null, "Газрын зургийн дэвсгэр ачаалагдахгүй байна. Жагсаалт хэвийн ажиллана.");
      }
    });
    tiles.addTo(map);

    var markers = {};
    var bounds = [];
    points.forEach(function (point) {
      var marker = L.marker([point.lat, point.lng], { title: point.title, keyboard: true })
        .addTo(map)
        .bindPopup(popupFor(point));
      marker.on("click", function () {
        var card = document.getElementById("job-" + point.code);
        if (card) {
          document.querySelectorAll(".job-card.active-card").forEach(function (c) { c.classList.remove("active-card"); });
          card.classList.add("active-card");
        }
      });
      markers[point.code] = marker;
      bounds.push([point.lat, point.lng]);
    });
    function fitToMarkers() {
      if (bounds.length > 1) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
      else if (bounds.length === 1) map.setView(bounds[0], 15);
    }
    // On phones the map starts hidden (list tab), where it has no size and can't be
    // fitted yet; in that case fit the first time it is shown.
    var fitted = container.offsetWidth > 0;
    if (fitted) fitToMarkers();

    // "Show on map" buttons in the list.
    document.querySelectorAll("[data-show-on-map]").forEach(function (button) {
      button.addEventListener("click", function () {
        var marker = markers[button.dataset.showOnMap];
        if (!marker) return;
        showView("map");
        map.setView(marker.getLatLng(), 16);
        marker.openPopup();
      });
    });

    // Leaflet needs a size recalculation when its hidden tab becomes visible.
    document.addEventListener("board-view-changed", function () {
      map.invalidateSize();
      if (!fitted && container.offsetWidth > 0) {
        fitted = true;
        fitToMarkers();
      }
    });

    var hash = window.location.hash.replace("#map-", "");
    if (markers[hash]) { showView("map"); markers[hash].openPopup(); map.setView(markers[hash].getLatLng(), 16); }
  }

  // List / map switch for small screens (both are shown side by side on wide screens).
  function showView(view) {
    var board = document.getElementById("board");
    if (!board) return;
    board.dataset.view = view;
    document.querySelectorAll("[data-view-button]").forEach(function (b) {
      var active = b.dataset.viewButton === view;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    document.dispatchEvent(new Event("board-view-changed"));
  }
  window.showBoardView = showView;

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-view-button]").forEach(function (b) {
      b.addEventListener("click", function () { showView(b.dataset.viewButton); });
    });
    try { init(); } catch (error) {
      showProblem(document.getElementById("job-map"), "Газрын зураг ачаалагдсангүй. Жагсаалтыг ашиглана уу.");
    }
  });
})();
