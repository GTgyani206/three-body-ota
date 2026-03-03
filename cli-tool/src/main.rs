use clap::Parser;
use sha2::{Digest, Sha256};
use std::error::Error;
use std::fs::{self, File};
use std::io::{self, BufReader, ErrorKind, Read};
use std::path::{Path, PathBuf};

#[derive(Debug, Parser)]
#[command(
    name = "firmware_signer",
    about = "Generate metadata.json for a firmware binary"
)]
struct Cli {
    #[arg(long, value_name = "PATH_TO_BIN")]
    file: PathBuf,
    #[arg(long, value_name = "VERSION_STRING")]
    version: String,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("Error: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let cli = Cli::parse();
    let (file_size_bytes, sha256_hash) = hash_file(&cli.file)?;
    let file_name = file_name_from_path(&cli.file)?;
    let metadata_path = metadata_output_path(&cli.file);

    let metadata_contents = format!(
        concat!(
            "{{\n",
            "  \"version\": {},\n",
            "  \"file_name\": {},\n",
            "  \"file_size_bytes\": {},\n",
            "  \"sha256_hash\": {}\n",
            "}}\n"
        ),
        serde_json::to_string(&cli.version)?,
        serde_json::to_string(&file_name)?,
        file_size_bytes,
        serde_json::to_string(&sha256_hash)?,
    );

    fs::write(&metadata_path, metadata_contents)?;
    println!("metadata.json generated at {}", metadata_path.display());
    Ok(())
}

fn hash_file(path: &Path) -> io::Result<(u64, String)> {
    let file = File::open(path)?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut total_bytes = 0_u64;
    let mut buffer = [0_u8; 8 * 1024];

    loop {
        let read_bytes = reader.read(&mut buffer)?;
        if read_bytes == 0 {
            break;
        }

        hasher.update(&buffer[..read_bytes]);
        let read_bytes_u64 = u64::try_from(read_bytes)
            .map_err(|_| io::Error::new(ErrorKind::InvalidData, "read size conversion failed"))?;
        total_bytes = total_bytes
            .checked_add(read_bytes_u64)
            .ok_or_else(|| io::Error::other("file size overflow"))?;
    }

    Ok((total_bytes, format!("{:x}", hasher.finalize())))
}

fn file_name_from_path(path: &Path) -> io::Result<String> {
    match path.file_name() {
        Some(name) => Ok(name.to_string_lossy().into_owned()),
        None => Err(io::Error::new(
            ErrorKind::InvalidInput,
            "input path does not contain a file name",
        )),
    }
}

fn metadata_output_path(input_path: &Path) -> PathBuf {
    let output_dir = match input_path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };

    output_dir.join("metadata.json")
}
