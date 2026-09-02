"use strict";

document.addEventListener("DOMContentLoaded", function () {
    var lastStatus = null;
    var pollInProgress = false;
    var startButton = document.getElementById("start-survey");
    var stopButton = document.getElementById("stop-survey");
    var fixedButton = document.getElementById("set-fixed");
    var copyButton = document.getElementById("copy-position");
    var alertElement = document.getElementById("operation-alert");

    function text(id, value) {
        document.getElementById(id).textContent = value;
    }

    function formatTime(seconds) {
        if (seconds === null || seconds === undefined) return "--";
        seconds = Math.max(0, Number(seconds));
        var hours = Math.floor(seconds / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var remainingSeconds = Math.floor(seconds % 60);
        var result = String(minutes).padStart(2, "0") + ":" + String(remainingSeconds).padStart(2, "0");
        return hours ? String(hours).padStart(2, "0") + ":" + result : result;
    }

    function showAlert(message, style) {
        alertElement.className = "alert alert-" + (style || "danger");
        alertElement.textContent = message;
        alertElement.hidden = !message;
    }

    function stateBadgeClass(state) {
        if (state === "complete" || state === "fixed") return "success";
        if (state === "error") return "danger";
        if (state === "starting" || state === "surveying" || state === "setting_fixed") return "warning";
        return "secondary";
    }

    function render(status) {
        lastStatus = status;
        var active = ["starting", "surveying", "setting_fixed"].indexOf(status.state) !== -1;
        var completed = status.state === "complete" || status.state === "fixed";
        var configured = status.supported_receiver && !status.configuration_error;
        var stateLabel = String(status.state || "idle").replace("_", " ").toUpperCase();
        var badge = document.getElementById("survey-state-badge");

        badge.textContent = stateLabel;
        badge.className = "badge badge-" + stateBadgeClass(status.state) + " px-3 py-2";
        text("receiver-name", status.receiver_name || "Quectel LC29H-BS");
        text("receiver-port", status.port || "Not configured");
        text("receiver-baud", status.baud || "Invalid");
        text("main-service", status.main_service_running === null ? "Unavailable" : (status.main_service_running ? "Running" : "Stopped"));
        text("survey-state", stateLabel);
        text("status-state", stateLabel);
        text("status-elapsed", formatTime(status.elapsed));
        text("status-remaining", formatTime(status.remaining));
        text("status-observations", status.observations === null ? "--" : status.observations);
        text("status-accuracy", status.mean_accuracy === null ? "--" : Number(status.mean_accuracy).toFixed(2) + " m");

        text("ecef-x", status.ecef ? Number(status.ecef.x).toFixed(4) : "--");
        text("ecef-y", status.ecef ? Number(status.ecef.y).toFixed(4) : "--");
        text("ecef-z", status.ecef ? Number(status.ecef.z).toFixed(4) : "--");
        text("latitude", status.geodetic ? Number(status.geodetic.latitude).toFixed(9) : "--");
        text("longitude", status.geodetic ? Number(status.geodetic.longitude).toFixed(9) : "--");
        text("ellipsoid-height", status.geodetic ? Number(status.geodetic.ellipsoid_height).toFixed(3) + " m" : "--");
        document.getElementById("rtkbase-position").value = status.rtkbase_position || "";

        startButton.disabled = active || !configured;
        stopButton.disabled = status.state !== "starting" && status.state !== "surveying";
        fixedButton.disabled = status.state !== "complete";
        copyButton.disabled = !completed || !status.rtkbase_position;
        document.getElementById("minimum-duration").disabled = active;
        document.getElementById("accuracy-limit").disabled = active;

        if (status.error) {
            showAlert(status.error, status.state === "stopped" ? "warning" : "danger");
        } else {
            showAlert("");
        }
    }

    function api(path, options) {
        return fetch(path, options || {}).then(function (response) {
            return response.json().then(function (body) {
                if (!response.ok) throw new Error(body.error || "Request failed.");
                return body;
            });
        });
    }

    function poll() {
        if (pollInProgress) return;
        pollInProgress = true;
        api("/api/v1/lc29h/survey/status")
            .then(render)
            .catch(function (error) { showAlert("Could not read survey status: " + error.message); })
            .finally(function () { pollInProgress = false; });
    }

    startButton.addEventListener("click", function () {
        var minimumDuration = Number(document.getElementById("minimum-duration").value);
        var accuracyLimit = Number(document.getElementById("accuracy-limit").value);
        api("/api/v1/lc29h/survey/start", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({minimum_duration: minimumDuration, accuracy_limit: accuracyLimit})
        }).then(render).catch(function (error) { showAlert(error.message); });
    });

    stopButton.addEventListener("click", function () {
        api("/api/v1/lc29h/survey/stop", {method: "POST"})
            .then(render).catch(function (error) { showAlert(error.message); });
    });

    fixedButton.addEventListener("click", function () {
        if (!window.confirm("Set the receiver to fixed mode using the final surveyed ECEF position?")) return;
        api("/api/v1/lc29h/survey/fixed", {method: "POST"})
            .then(render).catch(function (error) { showAlert(error.message); });
    });

    copyButton.addEventListener("click", function () {
        var position = document.getElementById("rtkbase-position").value;
        function copied() { showAlert("RTKBase position copied to the clipboard.", "success"); }
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(position).then(copied)
                .catch(function (error) { showAlert("Copy failed: " + error.message); });
        } else {
            var field = document.getElementById("rtkbase-position");
            field.select();
            if (document.execCommand("copy")) copied();
            else showAlert("Copy failed. Select the RTKBase position and copy it manually.");
            window.getSelection().removeAllRanges();
        }
    });

    poll();
    window.setInterval(poll, 1000);
});
