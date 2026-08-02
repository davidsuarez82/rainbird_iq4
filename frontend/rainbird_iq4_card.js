class RainBirdIQ4Card extends HTMLElement {
  static getStubConfig() {
    return {
      type: "custom:rainbird-iq4-card",
      title: "Garden irrigation",
      auto: true,
      default_duration: 10,
      show_programs: true,
    };
  }

  static getConfigElement() {
    return document.createElement("rainbird-iq4-card-editor");
  }

  setConfig(config) {
    this._config = {
      title: "Rain Bird IQ4",
      auto: true,
      default_duration: 10,
      show_programs: true,
      ...config,
    };
    this._selectedControllerId = this._config.controller_id
      ? String(this._config.controller_id)
      : this._selectedControllerId || null;
    this._stationDurations = this._stationDurations || {};
    this._stationActions = this._stationActions || {};
    this._refreshingUntil = this._refreshingUntil || 0;
    this._lastRefreshAt = this._lastRefreshAt || {};
    this._scheduledRefreshAt = this._scheduledRefreshAt || {};
    this._scheduledRefreshTimers = this._scheduledRefreshTimers || {};
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
  }

  set hass(hass) {
    this._hass = hass;
    this._reconcileStationActions();
    this._render();
  }

  getCardSize() {
    const stations = this._getStations();
    return Math.max(4, Math.min(10, stations.length + 4));
  }

  _render(force = false) {
    if (!this.shadowRoot || !this._hass) return;

    const stations = this._getStations();
    const controllers = this._getControllers(stations);
    if (!this._selectedControllerId && controllers.length) {
      this._selectedControllerId = controllers[0].id;
    }
    if (
      this._selectedControllerId &&
      controllers.length &&
      !controllers.some((controller) => controller.id === this._selectedControllerId)
    ) {
      this._selectedControllerId = controllers[0].id;
    }

    const selectedController =
      controllers.find((controller) => controller.id === this._selectedControllerId) ||
      controllers[0];
    const visibleStations = stations.filter(
      (station) => !selectedController || station.controllerId === selectedController.id
    );
    const renderKey = this._buildRenderKey(stations, controllers, selectedController);
    this._syncTicker();

    if (!force && renderKey === this._lastRenderKey) {
      return;
    }

    if (!force && this._isEditingControl()) {
      this._scheduleDeferredRender();
      return;
    }

    this._lastRenderKey = renderKey;

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          letter-spacing: 0;
        }

        ha-card {
          container-type: inline-size;
          overflow: hidden;
        }

        .wrap {
          display: grid;
          gap: 16px;
          padding: 18px;
        }

        .top {
          align-items: start;
          display: flex;
          gap: 14px;
          justify-content: space-between;
        }

        .heading {
          min-width: 0;
        }

        .title-line {
          align-items: center;
          display: flex;
          gap: 10px;
          min-width: 0;
        }

        .title-line ha-icon {
          --mdc-icon-size: 24px;
          color: var(--primary-color);
          flex: 0 0 auto;
        }

        h2 {
          color: var(--primary-text-color);
          font-size: 20px;
          font-weight: 700;
          line-height: 26px;
          margin: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .subtitle {
          color: var(--secondary-text-color);
          font-size: 13px;
          line-height: 18px;
          margin-top: 4px;
        }

        .top-actions {
          align-items: center;
          display: flex;
          flex: 0 0 auto;
          flex-wrap: wrap;
          gap: 8px;
          justify-content: end;
        }

        .controller-field {
          min-width: 180px;
        }

        select,
        input {
          background: var(--secondary-background-color);
          border: 1px solid transparent;
          border-radius: 8px;
          box-sizing: border-box;
          color: var(--primary-text-color);
          font: inherit;
          height: 40px;
          min-width: 0;
          outline: none;
          padding: 0 12px;
          width: 100%;
        }

        select:focus,
        input:focus {
          border-color: color-mix(in srgb, var(--primary-color) 55%, transparent);
          box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-color) 18%, transparent);
        }

        button {
          align-items: center;
          background: var(--primary-color);
          border: 0;
          border-radius: 999px;
          color: var(--text-primary-color, white);
          cursor: pointer;
          display: inline-flex;
          font: inherit;
          font-size: 13px;
          font-weight: 700;
          gap: 6px;
          height: 40px;
          justify-content: center;
          padding: 0 14px;
          white-space: nowrap;
        }

        button:active:not([disabled]) {
          transform: scale(0.98);
        }

        button ha-icon {
          --mdc-icon-size: 18px;
        }

        button.secondary {
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
        }

        button.danger {
          background: var(--error-color, #db4437);
          color: white;
        }

        button.icon {
          min-width: 40px;
          padding: 0;
        }

        button[disabled] {
          cursor: not-allowed;
          opacity: 0.45;
        }

        .pills {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
        }

        .pill {
          align-items: center;
          background: var(--secondary-background-color);
          border: 1px solid color-mix(in srgb, var(--divider-color) 70%, transparent);
          border-radius: 999px;
          color: var(--secondary-text-color);
          cursor: pointer;
          display: inline-flex;
          font-size: 12px;
          gap: 6px;
          min-height: 26px;
          padding: 0 10px;
        }

        .pill:hover {
          filter: brightness(0.95);
        }

        .pill ha-icon {
          --mdc-icon-size: 15px;
        }

        .pill.ok {
          background: color-mix(in srgb, var(--success-color, #43a047) 12%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--success-color, #43a047) 30%, transparent);
          color: var(--primary-text-color);
        }

        .pill.warn {
          background: color-mix(in srgb, var(--warning-color, #f4b400) 14%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--warning-color, #f4b400) 34%, transparent);
          color: var(--primary-text-color);
        }

        .pill.bad {
          background: color-mix(in srgb, var(--error-color, #db4437) 12%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--error-color, #db4437) 32%, transparent);
          color: var(--primary-text-color);
        }

        .pill.live {
          background: color-mix(in srgb, var(--success-color, #43a047) 20%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--success-color, #43a047) 46%, transparent);
          color: var(--primary-text-color);
          font-weight: 700;
        }

        .section {
          display: grid;
          gap: 10px;
        }

        .section-title {
          align-items: center;
          color: var(--secondary-text-color);
          display: flex;
          font-size: 12px;
          font-weight: 700;
          gap: 7px;
          text-transform: uppercase;
        }

        .section-title ha-icon {
          --mdc-icon-size: 15px;
        }

        .station-list,
        .program-list {
          display: grid;
          gap: 8px;
        }

        .station,
        .program {
          background: color-mix(in srgb, var(--secondary-background-color) 76%, transparent);
          border: 1px solid color-mix(in srgb, var(--divider-color) 72%, transparent);
          border-radius: 8px;
          cursor: pointer;
          display: grid;
          min-width: 0;
          padding: 12px;
        }

        .station {
          grid-template-columns: minmax(0, 1fr);
        }

        .station.is-running {
          background: color-mix(in srgb, var(--success-color, #43a047) 12%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--success-color, #43a047) 34%, transparent);
        }

        .station.is-paused {
          background: color-mix(in srgb, var(--warning-color, #f4b400) 13%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--warning-color, #f4b400) 34%, transparent);
        }

        .station.is-pending {
          background: color-mix(in srgb, var(--primary-color) 12%, var(--secondary-background-color));
          border-color: color-mix(in srgb, var(--primary-color) 36%, transparent);
        }

        .station-main {
          align-items: start;
          display: grid;
          gap: 12px;
          grid-template-columns: 34px minmax(0, 1fr);
          min-width: 0;
        }

        .station-main > div:last-child {
          min-width: 0;
        }

        .station-body {
          display: grid;
          gap: 4px;
          min-width: 0;
        }

        .station-head {
          align-items: center;
          display: grid;
          gap: 12px;
          grid-template-columns: minmax(0, 1fr) max-content;
          min-width: 0;
        }

        .terminal {
          align-items: center;
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
          border-radius: 50%;
          color: var(--secondary-text-color);
          display: inline-flex;
          font-size: 13px;
          font-weight: 700;
          height: 34px;
          justify-content: center;
          width: 34px;
        }

        .station.is-running .terminal {
          border-color: color-mix(in srgb, var(--success-color, #43a047) 42%, transparent);
          color: var(--success-color, #43a047);
        }

        .station-name {
          color: var(--primary-text-color);
          font-size: 15px;
          font-weight: 700;
          line-height: 20px;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .meta {
          color: var(--secondary-text-color);
          display: flex;
          flex-wrap: wrap;
          font-size: 12px;
          gap: 6px 9px;
          line-height: 17px;
          min-width: 0;
        }

        .meta.last-run {
          color: color-mix(in srgb, var(--secondary-text-color) 82%, transparent);
        }

        .meta .running {
          color: var(--success-color, #43a047);
          font-weight: 700;
        }

        .meta .paused {
          color: var(--warning-color, #f4b400);
          font-weight: 700;
        }

        .meta .pending {
          color: var(--primary-color);
          font-weight: 700;
        }

        .meta .error-text {
          color: var(--error-color, #db4437);
          font-weight: 700;
        }

        .station-actions {
          align-items: center;
          display: grid;
          gap: 8px;
          grid-template-columns: 96px auto;
          justify-content: end;
          min-width: 0;
        }

        .station-actions button {
          justify-self: end;
          min-width: 74px;
        }

        .duration {
          align-items: center;
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          display: grid;
          grid-template-columns: 48px auto;
          height: 40px;
          width: 96px;
          min-width: 96px;
          overflow: hidden;
        }

        .duration input {
          background: transparent;
          border: 0;
          box-shadow: none;
          height: 38px;
          padding: 0 4px 0 8px;
          text-align: right;
        }

        .duration span {
          color: var(--secondary-text-color);
          font-size: 12px;
          padding-right: 8px;
        }

        .spin {
          animation: rainbird-spin 0.9s linear infinite;
        }

        @keyframes rainbird-spin {
          to {
            transform: rotate(360deg);
          }
        }

        .program {
          grid-template-columns: minmax(0, 1fr) auto;
        }

        .program-name {
          color: var(--primary-text-color);
          font-size: 14px;
          font-weight: 700;
          line-height: 19px;
        }

        .program-status {
          border-radius: 999px;
          font-size: 12px;
          font-weight: 700;
          justify-self: start;
          padding: 4px 9px;
          text-transform: capitalize;
        }

        .program-status.scheduled {
          background: color-mix(in srgb, var(--success-color, #43a047) 14%, var(--secondary-background-color));
          color: var(--success-color, #43a047);
        }

        .program-status.off {
          background: var(--secondary-background-color);
          color: var(--secondary-text-color);
        }

        .empty,
        .error {
          background: var(--secondary-background-color);
          border-radius: 8px;
          color: var(--secondary-text-color);
          line-height: 20px;
          padding: 14px;
        }

        .error {
          background: color-mix(in srgb, var(--error-color, #db4437) 12%, var(--secondary-background-color));
          color: var(--primary-text-color);
        }

        @media (max-width: 760px) {
          .wrap {
            padding: 16px;
          }

          .top {
            display: grid;
          }

          .top-actions {
            justify-content: start;
          }

          .controller-field {
            min-width: min(100%, 260px);
            width: 100%;
          }

          .station,
          .program {
            grid-template-columns: minmax(0, 1fr);
          }

          .station-actions {
            grid-template-columns: 96px auto;
            justify-content: end;
          }
        }

        @media (max-width: 420px) {
          .station-head {
            gap: 10px;
          }

          .station-actions {
            grid-template-columns: 82px auto;
          }

          .duration {
            grid-template-columns: 40px auto;
            min-width: 82px;
            width: 82px;
          }

          .station-actions button {
            min-width: 64px;
            padding: 0 11px;
          }
        }

        @container (max-width: 560px) {
          .station,
          .program {
            grid-template-columns: minmax(0, 1fr);
          }

          .station-actions {
            grid-template-columns: 96px auto;
            justify-content: end;
          }
        }

        @container (max-width: 360px) {
          .station-main {
            gap: 10px;
          }

          .station-head {
            gap: 8px;
          }

          .station-actions {
            grid-template-columns: 82px auto;
          }

          .duration {
            grid-template-columns: 40px auto;
            min-width: 82px;
            width: 82px;
          }

          .station-actions button {
            min-width: 64px;
            padding: 0 11px;
          }
        }
      </style>

      <ha-card>
        <div class="wrap">
          <div class="top">
            <div class="heading">
              <div class="title-line">
                <ha-icon icon="mdi:sprinkler-variant"></ha-icon>
                <h2>${this._escape(this._config.title)}</h2>
              </div>
              <div class="subtitle">${this._escape(this._subtitle(visibleStations, selectedController))}</div>
            </div>
            <div class="top-actions">
              ${this._renderControllerPicker(controllers)}
              ${this._renderTopActions(selectedController, visibleStations)}
            </div>
          </div>
          ${
            this._actionError
              ? `<div class="error">${this._escape(this._actionError)}</div>`
              : ""
          }
          ${selectedController ? this._renderPills(selectedController) : ""}
          ${
            stations.length
              ? this._renderStations(visibleStations)
              : this._renderEmpty()
          }
          ${
            selectedController && this._config.show_programs !== false
              ? this._renderPrograms(selectedController)
              : ""
          }
        </div>
      </ha-card>
    `;

    this._bindEvents();
  }

  _renderControllerPicker(controllers) {
    if (controllers.length <= 1) return "";
    return `
      <select class="controller-field" data-controller aria-label="Controller">
        ${controllers
          .map(
            (controller) => `
              <option value="${this._escape(controller.id)}" ${controller.id === this._selectedControllerId ? "selected" : ""}>
                ${this._escape(controller.name)}
              </option>
            `
          )
          .join("")}
      </select>
    `;
  }

  _renderTopActions(controller, stations) {
    if (!controller) return "";
    const runningStations = stations.filter((station) => this._stationNeedsStop(station));
    const refreshing = this._isRefreshing();
    return `
      ${
        controller.refreshEntity
          ? `
            <button class="secondary ${refreshing ? "" : "icon"}" data-refresh title="Refresh Rain Bird data" aria-label="Refresh Rain Bird data" ${refreshing ? "disabled" : ""}>
              <ha-icon class="${refreshing ? "spin" : ""}" icon="mdi:refresh"></ha-icon>
              ${refreshing ? "Refreshing" : ""}
            </button>
          `
          : ""
      }
      ${
        runningStations.length
          ? `
            <button class="danger" data-stop-all>
              <ha-icon icon="mdi:stop-circle-outline"></ha-icon>
              Stop all
            </button>
          `
          : ""
      }
    `;
  }

  _renderPills(controller) {
    const pills = [];
    const runningCount = this._visibleStations().filter((station) =>
      this._stationAppearsRunning(station)
    ).length;
    if (runningCount) {
      pills.push({
        icon: "mdi:sprinkler-variant",
        tone: "live",
        text: `${runningCount} zone${runningCount === 1 ? "" : "s"} running`,
      });
    }
    if (controller.connected !== undefined) {
      pills.push({
        icon: controller.connected ? "mdi:cloud-check" : "mdi:cloud-alert",
        tone: controller.connected ? "ok" : "bad",
        text: controller.connected ? `${controller.name} online` : `${controller.name} offline`,
        entityId: controller.connectedState?.entity_id,
      });
    }
    if (controller.modeState && !this._isUnknown(controller.modeState.state)) {
      pills.push({
        icon: "mdi:controller",
        tone: "",
        text: `Mode: ${controller.modeState.state}`,
        entityId: controller.modeState?.entity_id,
      });
    }
    if (controller.rainDelayState && !this._isUnknown(controller.rainDelayState.state)) {
      const days = Number(controller.rainDelayState.state || 0);
      pills.push({
        icon: "mdi:weather-rainy",
        tone: days > 0 ? "warn" : "",
        text: days > 0 ? `Rain pause ${days}d` : "No rain pause",
        entityId: controller.rainDelayState?.entity_id,
      });
    }
    if (controller.forecastState) {
      pills.push({
        icon: "mdi:weather-partly-rainy",
        tone: controller.forecastState.state === "on" ? "ok" : "",
        text: controller.forecastState.state === "on" ? "Forecast delay on" : "Forecast delay off",
        entityId: controller.forecastState?.entity_id,
      });
    }

    const alarms = this._stateNumber(controller.alarmsState);
    const warnings = this._stateNumber(controller.warningsState);
    if (alarms > 0) {
      pills.push({ icon: "mdi:alarm-light", tone: "bad", text: `${alarms} alarms`, entityId: controller.alarmsState?.entity_id });
    }
    if (warnings > 0) {
      pills.push({ icon: "mdi:alert", tone: "warn", text: `${warnings} warnings`, entityId: controller.warningsState?.entity_id });
    }
    if (alarms === 0 && warnings === 0) {
      pills.push({ icon: "mdi:check-circle-outline", tone: "ok", text: "No alerts" });
    }

    if (!pills.length) return "";
    return `
      <div class="pills">
        ${pills
          .map(
            (pill) => `
              <span class="pill ${this._escape(pill.tone)}" ${pill.entityId ? `data-more-info="${this._escape(pill.entityId)}"` : ""}>
                <ha-icon icon="${this._escape(pill.icon)}"></ha-icon>
                ${this._escape(pill.text)}
              </span>
            `
          )
          .join("")}
      </div>
    `;
  }

  _renderStations(stations) {
    if (!stations.length) {
      return `<div class="empty">No stations found for this controller.</div>`;
    }
    return `
      <section class="section">
        <div class="section-title">
          <ha-icon icon="mdi:sprinkler"></ha-icon>
          Zones
        </div>
        <div class="station-list">
          ${stations.map((station) => this._renderStation(station)).join("")}
        </div>
      </section>
    `;
  }

  _renderStation(station) {
    const action = this._stationAction(station);
    const starting = action?.type === "starting";
    const stopping = action?.type === "stopping";
    const actionError = action?.type === "error";
    const running = !stopping && this._stationAppearsRunning(station);
    const paused = !stopping && station.state === "paused";
    const disabled = this._isUnknown(station.state);
    const duration = this._durationForStation(station);
    const maxDuration = this._maxDurationForStation(station);
    const remaining = this._stationRemaining(station, action);
    const last = this._formatLastRun(station.attributes.last_run_completed || station.attributes.last_run);
    const statusMeta = [
      station.terminal ? `Terminal ${station.terminal}` : null,
      starting ? `<span class="pending">Starting...</span>` : null,
      stopping ? `<span class="pending">Stopping...</span>` : null,
      actionError ? `<span class="error-text">${this._escape(action.message || "Command failed")}</span>` : null,
      running && remaining ? `<span class="running">${this._escape(remaining)} left</span>` : null,
      running && !remaining ? `<span class="running">Running</span>` : null,
      paused ? `<span class="paused">Paused</span>` : null,
      !running && !paused && !disabled && !stopping && !starting ? "Idle" : null,
      disabled ? this._humanState(station.state) : null,
    ]
      .filter(Boolean)
      .join("<span>•</span>");
    const lastMeta = last ? `Last ${this._escape(last)}` : "";
    const actions = `
      <div class="station-actions">
        ${
          this._stationNeedsStop(station) || starting || stopping
            ? ""
            : `
              <label class="duration" title="Run duration in minutes">
                <input data-station-duration="${this._escape(station.key)}" type="number" min="1" max="${maxDuration}" inputmode="numeric" value="${this._escape(duration)}" ${disabled || actionError ? "disabled" : ""}>
                <span>min</span>
              </label>
            `
        }
        ${
          stopping
            ? `
              <button class="danger" title="Stopping ${this._escape(station.name)}" disabled>
                <ha-icon class="spin" icon="mdi:loading"></ha-icon>
                Stopping
              </button>
            `
            : starting
              ? `
                <button title="Starting ${this._escape(station.name)}" disabled>
                  <ha-icon class="spin" icon="mdi:loading"></ha-icon>
                  Starting
                </button>
              `
              : this._stationNeedsStop(station)
            ? `
              <button class="danger" title="Stop ${this._escape(station.name)}" data-stop="${this._escape(station.key)}">
                <ha-icon icon="mdi:stop"></ha-icon>
                Stop
              </button>
            `
            : `
              <button title="Run ${this._escape(station.name)}" data-start="${this._escape(station.key)}" ${disabled || actionError ? "disabled" : ""}>
                <ha-icon icon="mdi:play"></ha-icon>
                Run
              </button>
            `
        }
      </div>
    `;

    return `
      <div class="station ${running ? "is-running" : ""} ${paused ? "is-paused" : ""} ${starting || stopping ? "is-pending" : ""}" data-more-info="${this._escape(station.entityId)}">
        <div class="station-main">
          <div class="terminal">${this._escape(station.terminal || "-")}</div>
          <div class="station-body">
            <div class="station-head">
              <div class="station-name">${this._escape(station.name)}</div>
              ${actions}
            </div>
            <div class="meta">${statusMeta}</div>
            ${lastMeta ? `<div class="meta last-run">${lastMeta}</div>` : ""}
          </div>
        </div>
      </div>
    `;
  }

  _renderPrograms(controller) {
    const programs = controller.programs || [];
    if (!programs.length) return "";
    return `
      <section class="section">
        <div class="section-title">
          <ha-icon icon="mdi:calendar-clock"></ha-icon>
          Schedule
        </div>
        <div class="program-list">
          ${programs.map((program) => this._renderProgram(program)).join("")}
        </div>
      </section>
    `;
  }

  // PATCH 1: support Cyclic / Odd / Even schedule types (v1.0.3 attributes)
  _renderProgram(program) {
    const attrs = program.attributes || {};
    const scheduled = program.state === "scheduled";
    const scheduleType = attrs.schedule_type || "weekly";

    const excluded = Array.isArray(attrs.excluded_days)
      ? attrs.excluded_days.join(", ")
      : attrs.excluded_days || "";

    let scheduleLine = null;
    if (scheduleType.startsWith("every ") && attrs.skip_days) {
      scheduleLine = excluded
        ? `Every ${attrs.skip_days} days (excl. ${excluded})`
        : `Every ${attrs.skip_days} days`;
    } else if (scheduleType === "odd days") {
      scheduleLine = excluded ? `Odd days (excl. ${excluded})` : "Odd days";
    } else if (scheduleType === "even days") {
      scheduleLine = excluded ? `Even days (excl. ${excluded})` : "Even days";
    } else {
      // weekly (default)
      scheduleLine = Array.isArray(attrs.week_days) && attrs.week_days.length
        ? this._formatWeekDays(attrs.week_days)
        : null;
    }

    const details = [
      attrs.start_time ? `Starts ${attrs.start_time}` : null,
      scheduleLine,
      attrs.next_run ? `Next: ${this._formatNextRun(attrs.next_run)}` : null,
      Number(attrs.steps || 0) > 0 ? `${attrs.steps} step${Number(attrs.steps) === 1 ? "" : "s"}` : null,
      attrs.weather_adjust ? `${attrs.weather_adjust} adjust` : null,
      attrs.seasonal_adjust !== undefined ? `${attrs.seasonal_adjust}%` : null,
    ]
      .filter(Boolean)
      .join("<span>•</span>");

    return `
      <div class="program" data-more-info="${this._escape(program.entity_id)}">
        <div>
          <div class="program-name">${this._escape(this._programName(program))}</div>
          <div class="meta">${details || "No schedule details"}</div>
        </div>
        <span class="program-status ${scheduled ? "scheduled" : "off"}">
          ${this._escape(this._humanState(program.state))}
        </span>
      </div>
    `;
  }

  _renderEmpty() {
    return `
      <div class="empty">
        No Rain Bird IQ4 zones were found. Add the integration, then refresh this dashboard.
      </div>
    `;
  }

  _bindEvents() {
    this.shadowRoot.querySelector("[data-controller]")?.addEventListener("change", (event) => {
      this._selectedControllerId = event.target.value;
      this._render(true);
    });

    this.shadowRoot.querySelectorAll("select, input").forEach((control) => {
      control.addEventListener("pointerdown", () => {
        this._deferRenderUntil = Date.now() + 1200;
      });
      control.addEventListener("focus", () => {
        this._deferRenderUntil = Date.now() + 1200;
      });
      control.addEventListener("blur", () => {
        this._deferRenderUntil = 0;
        this._render(true);
      });
    });

    this.shadowRoot.querySelector("[data-refresh]")?.addEventListener("click", () => {
      const controller = this._selectedController();
      if (!controller) return;
      this._refreshController(controller);
    });

    this.shadowRoot.querySelector("[data-stop-all]")?.addEventListener("click", () => {
      this._visibleStations()
        .filter((station) => this._stationNeedsStop(station))
        .forEach((station) => this._stopStation(station));
    });

    this.shadowRoot.querySelectorAll("[data-station-duration]").forEach((input) => {
      input.addEventListener("change", () => {
        const station = this._stationByKey(input.dataset.stationDuration);
        const duration = this._normalizeDuration(input.value, station);
        this._stationDurations[input.dataset.stationDuration] = duration;
        input.value = duration;
      });
    });

    this.shadowRoot.querySelectorAll("[data-start]").forEach((button) => {
      button.addEventListener("pointerdown", () => {
        button.dataset.pointerStarted = "true";
        this._handleStartButton(button);
      });

      button.addEventListener("click", () => {
        if (button.dataset.pointerStarted === "true") {
          return;
        }
        this._handleStartButton(button);
      });
    });

    this.shadowRoot.querySelectorAll("[data-stop]").forEach((button) => {
      button.addEventListener("click", () => {
        const station = this._stationByKey(button.dataset.stop);
        if (station) this._stopStation(station);
      });
    });

    this.shadowRoot.querySelectorAll("[data-more-info]").forEach((el) => {
      el.addEventListener("click", (event) => {
        // Don't fire more-info if clicking a button or input inside the element
        if (event.target.closest("button, input, select, label")) return;
        const entityId = el.dataset.moreInfo;
        if (entityId) this._fireMoreInfo(entityId);
      });
    });
  }

  _handleStartButton(button) {
    try {
      const station = this._stationByKey(button.dataset.start);
      if (!station || this._stationNeedsStop(station)) return;
      const input = this._durationInputForStation(station.key);
      const duration = this._normalizeDuration(input?.value, station);
      this._stationDurations[station.key] = duration;
      if (input) input.value = duration;
      this._startStation(station, duration);
    } catch (error) {
      this._actionError = error?.message || String(error || "Could not start zone");
      this._render(true);
    }
  }

  _startStation(station, duration) {
    this._setStationAction(station, {
      type: "starting",
      duration,
      requestedAt: Date.now(),
      settleUntil: Date.now() + 12000,
      expiresAt: Date.now() + duration * 60000 + 45000,
    });
    this._render(true);
    if (station.mode === "sensor") {
      this._callService("rainbird_iq4", "start_zone", {
        station_entity: station.entityId,
        duration,
      }, {
        success: () => {
          this._setStationAction(station, {
            type: "running",
            duration,
            requestedAt: Date.now(),
            settleUntil: Date.now() + 20000,
            expiresAt: Date.now() + duration * 60000 + 45000,
          });
          this._queueRefreshControllerById(station.controllerId, {
            delayMs: this._startRefreshDelayMs(),
          });
        },
        error: (error) => this._setStationError(station, error),
      });
      return;
    }
    this._callService("rainbird_iq4", "start_station", {
      station_id: Number(station.stationId),
      duration,
    }, {
      success: () => {
        this._setStationAction(station, {
          type: "running",
          duration,
          requestedAt: Date.now(),
          settleUntil: Date.now() + 20000,
          expiresAt: Date.now() + duration * 60000 + 45000,
        });
        this._queueRefreshControllerById(station.controllerId, {
          delayMs: this._startRefreshDelayMs(),
        });
      },
      error: (error) => this._setStationError(station, error),
    });
  }

  _stopStation(station) {
    this._setStationAction(station, {
      type: "stopping",
      requestedAt: Date.now(),
      settleUntil: Date.now() + 6000,
      expiresAt: Date.now() + 45000,
    });
    this._render(true);
    if (station.mode === "sensor") {
      this._callService("rainbird_iq4", "stop_zone", {
        station_entity: station.entityId,
      }, {
        success: () => this._queueRefreshControllerById(station.controllerId, {
          delayMs: this._stopRefreshDelayMs(),
        }),
        error: (error) => this._setStationError(station, error),
      });
      return;
    }
    this._callService("rainbird_iq4", "stop_station", {
      station_id: Number(station.stationId),
    }, {
      success: () => this._queueRefreshControllerById(station.controllerId, {
        delayMs: this._stopRefreshDelayMs(),
      }),
      error: (error) => this._setStationError(station, error),
    });
  }

  _callService(domain, service, data, callbacks = {}) {
    this._actionError = "";
    try {
      const result = this._hass.callService(domain, service, data);
      Promise.resolve(result)
        .then(() => callbacks.success?.())
        .catch((error) => {
          callbacks.error?.(error);
          this._actionError = error?.message || String(error || "Service call failed");
          this._render(true);
        });
    } catch (error) {
      callbacks.error?.(error);
      this._actionError = error?.message || String(error || "Service call failed");
      this._render(true);
    }
  }

  _refreshController(controller) {
    this._queueRefreshController(controller);
  }

  _queueRefreshController(controller, options = {}) {
    if (!controller?.refreshEntity) return;
    const now = Date.now();
    const delayMs = Math.max(0, Number(options.delayMs || 0));
    const throttleUntil = (this._lastRefreshAt[controller.id] || 0) + this._refreshThrottleMs();
    const runAt = Math.max(now + delayMs, throttleUntil);
    const scheduledAt = this._scheduledRefreshAt[controller.id];
    if (scheduledAt && scheduledAt <= runAt) {
      this._refreshingUntil = Math.max(this._refreshingUntil || 0, scheduledAt + 5000);
      this._render(true);
      return;
    }
    if (this._scheduledRefreshTimers[controller.id]) {
      window.clearTimeout(this._scheduledRefreshTimers[controller.id]);
    }
    this._scheduledRefreshAt[controller.id] = runAt;
    this._refreshingUntil = Math.max(this._refreshingUntil || 0, runAt + 5000);
    this._render(true);
    this._scheduledRefreshTimers[controller.id] = window.setTimeout(() => {
      this._performRefresh(controller.id);
    }, Math.max(0, runAt - now));
    this._syncTicker();
  }

  _performRefresh(controllerId) {
    delete this._scheduledRefreshAt[controllerId];
    delete this._scheduledRefreshTimers[controllerId];
    const controller = this._getControllers(this._getStations()).find(
      (item) => item.id === controllerId
    );
    if (!controller?.refreshEntity) return;
    this._lastRefreshAt[controller.id] = Date.now();
    this._refreshingUntil = Date.now() + 5000;
    this._callService("button", "press", { entity_id: controller.refreshEntity }, {
      success: () => {
        this._refreshingUntil = Date.now() + 5000;
        this._render(true);
      },
      error: (error) => {
        this._actionError = error?.message || String(error || "Service call failed");
        this._lastRefreshAt[controller.id] = 0;
        this._refreshingUntil = 0;
        this._render(true);
      },
    });
  }

  _queueRefreshControllerById(controllerId, options = {}) {
    const controller = this._getControllers(this._getStations()).find(
      (item) => item.id === controllerId
    );
    if (controller?.refreshEntity) {
      this._queueRefreshController(controller, options);
    }
  }

  _refreshThrottleMs() {
    return Math.max(5, Number(this._config.refresh_throttle_seconds || 30)) * 1000;
  }

  _startRefreshDelayMs() {
    return Math.max(0, Number(this._config.start_refresh_delay_seconds || 8)) * 1000;
  }

  _stopRefreshDelayMs() {
    return Math.max(0, Number(this._config.stop_refresh_delay_seconds || 5)) * 1000;
  }

  _selectedController() {
    return this._getControllers(this._getStations()).find(
      (controller) => controller.id === this._selectedControllerId
    );
  }

  _visibleStations() {
    const stations = this._getStations();
    const selected = this._selectedController();
    return stations.filter((station) => !selected || station.controllerId === selected.id);
  }

  _stationByKey(key) {
    return this._getStations().find((station) => station.key === key);
  }

  _durationInputForStation(stationKey) {
    return [...this.shadowRoot.querySelectorAll("[data-station-duration]")].find(
      (input) => input.dataset.stationDuration === stationKey
    );
  }

  _getStations() {
    if (!this._hass) return [];
    const configuredEntities = Array.isArray(this._config.entities)
      ? this._config.entities
      : [];
    if (configuredEntities.length) {
      return configuredEntities
        .map((entityId) => [entityId, this._hass.states[entityId]])
        .filter(([, state]) => state)
        .map(([entityId, state]) => this._stationFromState(entityId, state))
        .filter(Boolean)
        .sort((left, right) => this._stationSort(left, right));
    }
    if (this._config.auto === false) return [];

    const sensorStations = Object.entries(this._hass.states)
      .filter(([entityId, state]) => this._isStationSensor(entityId, state))
      .map(([entityId, state]) => this._stationFromState(entityId, state))
      .filter(Boolean);

    if (sensorStations.length) {
      const filtered = this._config.hide_inactive_stations
        ? sensorStations.filter((s) => s.attributes?.is_active !== false)
        : sensorStations;
      return filtered.sort((left, right) => this._stationSort(left, right));
    }

    return Object.entries(this._hass.states)
      .filter(([entityId, state]) => this._isLegacyStationSwitch(entityId, state))
      .map(([entityId, state]) => this._stationFromState(entityId, state))
      .filter(Boolean)
      .sort((left, right) => this._stationSort(left, right));
  }

  _stationFromState(entityId, state) {
    if (!state) return null;
    const attrs = state.attributes || {};
    if (this._isStationSensor(entityId, state)) {
      const controllerId = this._controllerIdForSensor(entityId);
      const name = this._cleanStationName(attrs.friendly_name || entityId, controllerId);
      return {
        mode: "sensor",
        key: entityId,
        entityId,
        state: state.state,
        attributes: attrs,
        stationId: entityId,
        controllerId,
        name,
        terminal: attrs.terminal,
      };
    }
    if (entityId.startsWith("switch.") && attrs.station_id !== undefined) {
      return {
        mode: "legacy",
        key: String(attrs.station_id),
        entityId,
        state: state.state,
        attributes: attrs,
        stationId: String(attrs.station_id),
        controllerId: String(attrs.controller_id),
        name: attrs.friendly_name || entityId,
        terminal: attrs.terminal,
      };
    }
    return null;
  }

  _isStationSensor(entityId, state) {
    if (!entityId.startsWith("sensor.")) return false;
    const attrs = state.attributes || {};
    if (attrs.terminal === undefined) return false;
    if (!["idle", "running", "paused", "unknown", "unavailable"].includes(state.state)) {
      return false;
    }
    if (entityId.includes("_program_") || entityId.includes("_rain_delay")) return false;
    return attrs.icon === "mdi:sprinkler" || attrs.device_class === "enum";
  }

  _isLegacyStationSwitch(entityId, state) {
    const attrs = state.attributes || {};
    return (
      entityId.startsWith("switch.") &&
      state.state !== "unavailable" &&
      attrs.station_id !== undefined &&
      attrs.controller_id !== undefined
    );
  }

  _getControllers(stations) {
    const ids = [...new Set(stations.map((station) => station.controllerId))];
    return ids.map((id) => {
      const controllerStations = stations.filter((station) => station.controllerId === id);
      const mode = controllerStations[0]?.mode || "sensor";
      const connectedState =
        mode === "sensor" ? this._entity(`binary_sensor.${id}_connected`) : this._legacyConnection(id);
      const controller = {
        id,
        mode,
        name: this._controllerName(id, connectedState, controllerStations),
        connected: connectedState ? connectedState.state === "on" : undefined,
        connectedState,
        rainDelayState:
          mode === "sensor" ? this._entity(`sensor.${id}_rain_delay`) : this._legacyRainDelay(id),
        forecastState: mode === "sensor" ? this._entity(`binary_sensor.${id}_forecast_rain_delay`) : null,
        alarmsState: mode === "sensor" ? this._findSensorByNeedle(id, "alarms") : null,
        warningsState: mode === "sensor" ? this._findSensorByNeedle(id, "warnings") : null,
        modeState: mode === "sensor" ? this._entity(`sensor.${id}_controller_mode`) : null,
        refreshEntity: mode === "sensor" ? this._findButtonByNeedle(id, "refresh")?.entity_id : null,
        programs: mode === "sensor" ? this._programsForController(id) : [],
      };
      return controller;
    });
  }

  _controllerName(id, connectedState, stations) {
    if (this._config.controller_names?.[id]) {
      return this._config.controller_names[id];
    }
    const friendly = connectedState?.attributes?.friendly_name;
    if (friendly) {
      const stripped = friendly.replace(/\s+(Connected|Connection)$/i, "").trim();
      if (stripped && stripped !== friendly) return stripped;
    }
    const firstName = stations[0]?.attributes?.friendly_name || "";
    const label = this._labelFromControllerId(id);
    if (firstName.toLowerCase().startsWith(`${label.toLowerCase()} `)) {
      return label;
    }
    return /^\d+$/.test(String(id)) ? `Controller ${id}` : label;
  }

  _controllerIdForSensor(entityId) {
    const objectId = entityId.split(".")[1] || entityId;
    const prefixes = this._controllerPrefixes();
    const matchedPrefix = prefixes
      .sort((left, right) => right.length - left.length)
      .find((prefix) => objectId.startsWith(`${prefix}_`));
    if (matchedPrefix) return matchedPrefix;

    const stationMatch = objectId.match(/^(.+?)_station_/);
    if (stationMatch) return stationMatch[1];
    return objectId.split("_")[0];
  }

  _controllerPrefixes() {
    if (!this._hass) return [];
    const prefixes = new Set();
    Object.keys(this._hass.states).forEach((entityId) => {
      const objectId = entityId.split(".")[1] || "";
      [
        /^(.+)_connected$/,
        /^(.+)_rain_delay$/,
        /^(.+)_forecast_rain_delay$/,
        /^(.+)_controller_mode$/,
        /^(.+)_program_.+_status$/,
      ].forEach((pattern) => {
        const match = objectId.match(pattern);
        if (match) prefixes.add(match[1]);
      });
    });
    return [...prefixes];
  }

  _programsForController(id) {
    return Object.values(this._hass.states)
      .filter((state) => {
        if (!state.entity_id?.startsWith(`sensor.${id}_program_`)) return false;
        if (!state.entity_id.endsWith("_status")) return false;
        if (this._config.hide_inactive_programs && state.state !== "scheduled") return false;
        return true;
      })
      .sort((left, right) => this._programName(left).localeCompare(this._programName(right)));
  }

  // PATCH 2: robust name cleaning — try all known controller prefixes (longest first),
  // then fall back to the resolved controller label, to handle area-prefixed entity IDs.
  _programName(program) {
    const name = program.attributes?.friendly_name || program.entity_id || "Program";
    const entityId = program.entity_id || "";
    const controllerId = this._controllerIdForSensor(entityId);

    // Try stripping any known controller prefix (snake_case → Title Case variants)
    const cleaned = this._stripControllerPrefix(name, controllerId);
    return cleaned.replace(/\s+Status$/i, "").trim();
  }

  // PATCH 2 (continued): shared helper used by both _cleanStationName and _programName
  _stripControllerPrefix(name, controllerId) {
    // Build candidate labels to try, longest first
    const candidates = [];

    // 1. The resolved controller label (e.g. "ESP-TM2" from friendly name)
    const connectedState = this._entity(`binary_sensor.${controllerId}_connected`);
    if (connectedState?.attributes?.friendly_name) {
      const label = connectedState.attributes.friendly_name
        .replace(/\s+(Connected|Connection)$/i, "")
        .trim();
      if (label) candidates.push(label);
    }

    // 2. All known controller prefixes converted to Title Case labels
    const prefixes = this._controllerPrefixes();
    prefixes
      .sort((a, b) => b.length - a.length)
      .forEach((prefix) => candidates.push(this._labelFromControllerId(prefix)));

    // 3. The controllerId itself as a label
    candidates.push(this._labelFromControllerId(controllerId));

    // Try each candidate as a prefix to strip
    for (const candidate of candidates) {
      const escaped = this._escapeRegExp(candidate);
      const stripped = String(name).replace(new RegExp(`^${escaped}\\s+`, "i"), "").trim();
      if (stripped && stripped !== name) return stripped;
    }

    return String(name).trim();
  }

  _cleanStationName(name, controllerId) {
    return this._stripControllerPrefix(name, controllerId);
  }

  _legacyConnection(controllerId) {
    return Object.values(this._hass.states).find((state) => {
      return (
        state.entity_id?.startsWith("binary_sensor.") &&
        String(state.attributes?.controller_id) === String(controllerId)
      );
    });
  }

  _legacyRainDelay(controllerId) {
    return Object.values(this._hass.states).find((state) => {
      return (
        state.entity_id?.startsWith("number.") &&
        String(state.attributes?.controller_id) === String(controllerId)
      );
    });
  }

  _entity(entityId) {
    return this._hass.states[entityId] || null;
  }

  _findSensorByNeedle(prefix, needle) {
    return Object.values(this._hass.states).find((state) => {
      return (
        state.entity_id?.startsWith(`sensor.${prefix}_`) &&
        state.entity_id.includes(needle)
      );
    });
  }

  _findButtonByNeedle(prefix, needle) {
    return Object.values(this._hass.states).find((state) => {
      return (
        state.entity_id?.startsWith(`button.${prefix}_`) &&
        state.entity_id.includes(needle)
      );
    });
  }

  _stationSort(left, right) {
    const terminalLeft = Number(left.terminal || 9999);
    const terminalRight = Number(right.terminal || 9999);
    return terminalLeft - terminalRight || left.name.localeCompare(right.name);
  }

  _subtitle(stations, controller) {
    if (!stations.length) return "Ready when your Rain Bird zones are available";
    const running = stations.filter((station) => this._stationAppearsRunning(station)).length;
    if (running) return `${running} zone${running === 1 ? "" : "s"} running`;
    const connected = controller?.connected;
    if (connected === false) return "Controller is offline";
    return `${stations.length} zone${stations.length === 1 ? "" : "s"} ready`;
  }

  _durationForStation(station) {
    return this._normalizeDuration(
      this._stationDurations?.[station.key] ?? this._config.default_duration ?? 10,
      station
    );
  }

  _normalizeDuration(value, station) {
    const max = this._maxDurationForStation(station);
    const number = Number(value || 1);
    if (!Number.isFinite(number)) return 1;
    return Math.max(1, Math.min(max, Math.round(number)));
  }

  _maxDurationForStation(station) {
    return station?.mode === "sensor" ? 30 : 720;
  }

  _stationAction(station) {
    const action = this._stationActions?.[station.key];
    if (!action) return null;
    if (action.expiresAt && Date.now() > action.expiresAt) {
      delete this._stationActions[station.key];
      return null;
    }
    return action;
  }

  _setStationAction(station, action) {
    this._stationActions = this._stationActions || {};
    this._stationActions[station.key] = action;
    this._syncTicker();
  }

  _setStationError(station, error) {
    this._setStationAction(station, {
      type: "error",
      message: error?.message || String(error || "Command failed"),
      expiresAt: Date.now() + 12000,
    });
    this._render(true);
  }

  _reconcileStationActions() {
    if (!this._stationActions || !Object.keys(this._stationActions).length) return;
    const stations = new Map(this._getStations().map((station) => [station.key, station]));
    Object.entries(this._stationActions).forEach(([key, action]) => {
      const station = stations.get(key);
      const now = Date.now();
      if (!station || (action.expiresAt && now > action.expiresAt)) {
        delete this._stationActions[key];
        return;
      }
      if (action.settleUntil && now < action.settleUntil) {
        return;
      }
      if ((action.type === "starting" || action.type === "running") && this._isStationRunning(station)) {
        delete this._stationActions[key];
        return;
      }
      if (action.type === "stopping" && !this._isStationRunning(station) && station.state !== "paused") {
        delete this._stationActions[key];
      }
    });
    this._syncTicker();
  }

  _isStationRunning(station) {
    return station.state === "running" || station.state === "on";
  }

  _stationAppearsRunning(station) {
    const action = this._stationAction(station);
    if (action?.type === "stopping") return false;
    if (action?.type === "starting" || action?.type === "running") return true;
    return this._isStationRunning(station);
  }

  _stationNeedsStop(station) {
    return this._stationAppearsRunning(station) || station.state === "paused";
  }

  _isUnknown(state) {
    return state === "unknown" || state === "unavailable";
  }

  _stateNumber(state) {
    if (!state || this._isUnknown(state.state)) return null;
    const number = Number(state.state);
    return Number.isFinite(number) ? number : null;
  }

  _humanState(state) {
    return String(state || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  _formatRemaining(value) {
    if (value === null || value === undefined || value === "") return "";
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "";
    const minutes = number > 720 ? Math.ceil(number / 60) : Math.ceil(number);
    return `${minutes} min`;
  }

  _stationRemaining(station, action) {
    const liveRemaining = this._formatRemaining(
      station.attributes.remaining ?? station.attributes.remaining_seconds
    );
    if (liveRemaining) return liveRemaining;
    if (
      (action?.type === "starting" || action?.type === "running") &&
      action.duration &&
      action.requestedAt
    ) {
      const remainingMs = action.requestedAt + action.duration * 60000 - Date.now();
      if (remainingMs > 0) {
        return `${Math.max(1, Math.ceil(remainingMs / 60000))} min`;
      }
    }
    return "";
  }

  _isRefreshing() {
    if (!this._refreshingUntil) return false;
    if (Date.now() > this._refreshingUntil) {
      this._refreshingUntil = 0;
      return false;
    }
    return true;
  }

  _hasTemporaryState() {
    return this._isRefreshing() || Boolean(Object.keys(this._stationActions || {}).length);
  }

  _syncTicker() {
    if (this._hasTemporaryState()) {
      if (this._ticker) return;
      this._ticker = window.setInterval(() => {
        this._reconcileStationActions();
        if (!this._hasTemporaryState()) {
          this._syncTicker();
        }
        this._render(true);
      }, 1000);
      return;
    }
    if (this._ticker) {
      window.clearInterval(this._ticker);
      this._ticker = null;
    }
  }

  _formatLastRun(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString(undefined, {
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      month: "short",
    });
  }


  _formatNextRun(value) {
    if (!value) return "";
    const next = new Date(value);
    if (isNaN(next.getTime())) return value;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    next.setHours(0, 0, 0, 0);
    const days = Math.round((next - today) / 86400000);
    if (days === 0) return "Today";
    if (days === 1) return "Tomorrow";
    if (days > 1) return `in ${days} days`;
    return value;
  }

  _formatWeekDays(days) {
    if (!Array.isArray(days) || !days.length) return "";
    if (days.length === 7) return "Every day";
    return days.join(", ");
  }

  // PATCH 2 (continued): _cleanStationName now delegates to _stripControllerPrefix
  // (defined above, replacing the old single-label approach)

  _labelFromControllerId(id) {
    const text = String(id || "")
      .replace(/_/g, " ")
      .trim();
    if (text.length <= 3) return text.toUpperCase();
    return text.replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  _buildRenderKey(stations, controllers, selectedController) {
    return JSON.stringify({
      config: this._config,
      selectedControllerId: selectedController?.id || null,
      actionError: this._actionError || "",
      temporary: this._hasTemporaryState() ? Math.floor(Date.now() / 1000) : 0,
      stationActions: Object.entries(this._stationActions || {}).map(([key, action]) => [
        key,
        action.type,
        action.duration,
        action.requestedAt,
        action.settleUntil,
        action.expiresAt,
        action.message,
      ]),
      refreshingUntil: this._refreshingUntil || 0,
      stations: stations.map((station) => [
        station.entityId,
        station.state,
        station.name,
        station.stationId,
        station.controllerId,
        station.terminal,
        station.attributes.remaining,
        station.attributes.remaining_seconds,
        station.attributes.last_run,
        station.attributes.last_run_completed,
      ]),
      controllers: controllers.map((controller) => [
        controller.id,
        controller.name,
        controller.connected,
        controller.rainDelayState?.state,
        controller.forecastState?.state,
        controller.alarmsState?.state,
        controller.warningsState?.state,
        controller.modeState?.state,
        controller.refreshEntity,
        ...(controller.programs || []).map((program) => [
          program.entity_id,
          program.state,
          program.attributes?.start_time,
          program.attributes?.steps,
          program.attributes?.weather_adjust,
          program.attributes?.seasonal_adjust,
          (program.attributes?.week_days || []).join(","),
          program.attributes?.schedule_type,
          program.attributes?.skip_days,
          program.attributes?.next_run,
        ]),
      ]),
    });
  }

  _isEditingControl() {
    if (this._deferRenderUntil && Date.now() < this._deferRenderUntil) {
      return true;
    }
    const activeElement = this.shadowRoot?.activeElement;
    return activeElement?.matches?.("select, input, textarea") || false;
  }

  _scheduleDeferredRender() {
    clearTimeout(this._deferredRenderTimer);
    this._deferredRenderTimer = setTimeout(() => {
      if (this._isEditingControl()) {
        this._scheduleDeferredRender();
        return;
      }
      this._render(true);
    }, 250);
  }

  _fireMoreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      bubbles: true,
      composed: true,
      detail: { entityId },
    }));
  }

  _escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
}

class RainBirdIQ4CardEditor extends HTMLElement {
  setConfig(config) {
    this._config = {
      auto: true,
      default_duration: 10,
      show_programs: true,
      ...config,
    };
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const entities = Array.isArray(this._config.entities)
      ? this._config.entities.join("\n")
      : "";
    this.shadowRoot.innerHTML = `
      <style>
        .editor {
          display: grid;
          gap: 12px;
          padding: 8px 0;
        }

        label {
          color: var(--secondary-text-color);
          display: block;
          font-size: 12px;
          margin-bottom: 4px;
        }

        input,
        textarea {
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          box-sizing: border-box;
          color: var(--primary-text-color);
          font: inherit;
          min-height: 38px;
          padding: 8px 10px;
          width: 100%;
        }

        textarea {
          min-height: 92px;
        }

        .check {
          align-items: center;
          display: flex;
          gap: 8px;
        }

        .check input {
          width: auto;
        }
      </style>
      <div class="editor">
        <div>
          <label>Title</label>
          <input data-key="title" value="${this._escape(this._config.title || "Rain Bird IQ4")}">
        </div>
        <div>
          <label>Default duration, minutes</label>
          <input data-key="default_duration" type="number" min="1" max="720" value="${this._escape(this._config.default_duration || 10)}">
        </div>
        <div>
          <label>Controller ID or prefix to select by default</label>
          <input data-key="controller_id" value="${this._escape(this._config.controller_id || "")}">
        </div>
        <label class="check">
          <input data-key="auto" type="checkbox" ${this._config.auto !== false ? "checked" : ""}>
          Auto-discover Rain Bird IQ4 zones
        </label>
        <label class="check">
          <input data-key="show_programs" type="checkbox" ${this._config.show_programs !== false ? "checked" : ""}>
          Show program schedule
        </label>
        <label class="check">
          <input data-key="hide_inactive_programs" type="checkbox" ${this._config.hide_inactive_programs ? "checked" : ""}>
          Hide inactive programs (not scheduled / disabled)
        </label>
        <label class="check">
          <input data-key="hide_inactive_stations" type="checkbox" ${this._config.hide_inactive_stations ? "checked" : ""}>
          Hide inactive stations (not assigned to any program)
        </label>
        <div>
          <label>Station entities, one per line. Leave empty for auto-discovery.</label>
          <textarea data-key="entities">${this._escape(entities)}</textarea>
        </div>
      </div>
    `;
    this._bindEvents();
  }

  _bindEvents() {
    this.shadowRoot.querySelectorAll("[data-key]").forEach((input) => {
      input.addEventListener("change", () => {
        const key = input.dataset.key;
        const config = { ...this._config };
        if (key === "auto" || key === "show_programs" || key === "hide_inactive_programs" || key === "hide_inactive_stations") {
          config[key] = input.checked;
        } else if (key === "default_duration") {
          const value = input.value === "" ? undefined : Number(input.value);
          if (value === undefined) delete config[key];
          else config[key] = value;
        } else if (key === "controller_id") {
          if (input.value === "") delete config[key];
          else config[key] = input.value;
        } else if (key === "entities") {
          const entities = input.value
            .split(/\n|,/)
            .map((item) => item.trim())
            .filter(Boolean);
          if (entities.length) config.entities = entities;
          else delete config.entities;
        } else {
          config[key] = input.value;
        }
        this._config = config;
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config },
            bubbles: true,
            composed: true,
          })
        );
      });
    });
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
}

customElements.define("rainbird-iq4-card", RainBirdIQ4Card);
customElements.define("rainbird-iq4-card-editor", RainBirdIQ4CardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "rainbird-iq4-card",
  name: "Rain Bird IQ4 Card",
  description: "Control Rain Bird IQ4 zones with per-zone durations and live schedule status.",
});
