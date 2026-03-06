use std::env;
use std::process::ExitCode;
use wolframe_spotify_canvas::CanvasClient;

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();

    if args.len() < 3 {
        eprintln!("Usage: canvas-finder <track_id> <access_token>");
        return ExitCode::from(2);
    }

    let track_id = &args[1];
    let access_token = &args[2];

    let track_uri = if track_id.starts_with("spotify:track:") {
        track_id.clone()
    } else {
        format!("spotify:track:{}", track_id)
    };

    let mut client = CanvasClient::new();

    match client.get_canvas(&track_uri, access_token).await {
        Ok(canvas) => {
            print!("{}", canvas.mp4_url);
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("canvas-finder error: {:?}", e);
            ExitCode::from(1)
        }
    }
}
