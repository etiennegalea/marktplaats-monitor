#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod api;
mod app;
mod matching;
mod model;
mod state;

use eframe::egui;

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("Marktplaats listing monitor")
            .with_inner_size([1280.0, 860.0])
            .with_min_inner_size([960.0, 620.0]),
        centered: true,
        ..Default::default()
    };
    eframe::run_native(
        "marktplaats-monitor",
        options,
        Box::new(|context| Ok(Box::new(app::MonitorApp::new(context)))),
    )
}
