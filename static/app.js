(function () {
    "use strict";

    // Aligne sur OVERVIEW_CACHE_TTL/FARMING_CACHE_TTL (db.py) - inutile de
    // retaper plus souvent, /  et /classement renverraient la meme reponse
    // en cache.
    var REFRESH_MS = 30000;

    var content = document.getElementById("page-content");
    var status = document.getElementById("live-status");

    function isEditingInsideContent() {
        var el = document.activeElement;
        if (!el || !content.contains(el)) {
            return false;
        }
        return el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA";
    }

    function setStatus(text) {
        if (status) {
            status.textContent = text;
        }
    }

    function refresh() {
        if (isEditingInsideContent()) {
            return; // ne pas ecraser un filtre en cours de saisie
        }
        fetch(window.location.href, { credentials: "same-origin" })
            .then(function (res) {
                if (!res.ok) {
                    throw new Error("HTTP " + res.status);
                }
                return res.text();
            })
            .then(function (html) {
                var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("page-content");
                if (fresh) {
                    content.innerHTML = fresh.innerHTML;
                    setStatus("Mis a jour a " + new Date().toLocaleTimeString("fr-FR"));
                }
            })
            .catch(function () {
                setStatus("Actualisation en pause (connexion)");
            });
    }

    if (content) {
        setStatus("Mis a jour a " + new Date().toLocaleTimeString("fr-FR"));
        setInterval(refresh, REFRESH_MS);
    }
})();
