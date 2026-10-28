/*
 * Admin listing form: tap the map to set the public pin; the latitude/longitude
 * inputs are filled in. Typing coordinates still works without the map.
 */
(function () {
  "use strict";
  document.addEventListener("DOMContentLoaded", function () {
    var container = document.getElementById("pin-map");
    var lat = document.getElementById("latitude");
    var lng = document.getElementById("longitude");
    if (!container || !lat || !lng) return;
    if (typeof L === "undefined") {
      container.textContent = "Газрын зураг ачаалагдсангүй. Координатыг гараар оруулна уу.";
      container.classList.add("map-failed");
      return;
    }
    var cfg = container.dataset;
    var hasPin = lat.value && lng.value;
    var start = hasPin ? [parseFloat(lat.value), parseFloat(lng.value)]
                       : [parseFloat(cfg.centerLat), parseFloat(cfg.centerLng)];
    var map = L.map(container).setView(start, hasPin ? 16 : parseInt(cfg.zoom, 10));
    L.tileLayer(cfg.tileUrl, { maxZoom: 18, attribution: cfg.attribution }).addTo(map);
    var marker = hasPin ? L.marker(start).addTo(map) : null;

    function place(latlng) {
      lat.value = latlng.lat.toFixed(6);
      lng.value = latlng.lng.toFixed(6);
      if (marker) marker.setLatLng(latlng); else marker = L.marker(latlng).addTo(map);
    }
    map.on("click", function (event) { place(event.latlng); });

    var clear = document.getElementById("clear-pin");
    if (clear) clear.addEventListener("click", function () {
      lat.value = ""; lng.value = "";
      if (marker) { map.removeLayer(marker); marker = null; }
    });
  });
})();
