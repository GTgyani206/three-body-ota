use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use clap::{Parser, Subcommand};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand::rngs::OsRng;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::error::Error;
use std::fs::{self, File};
use std::io::{self, BufReader, ErrorKind, Read};
use std::path::{Path, PathBuf};

#[derive(Debug, Parser)]
#[command(
    name = "firmware_signer",
    about = "Sign and verify ESP32 firmware binaries for Three-Body OTA",
    version
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Sign a .bin firmware and generate signed metadata JSON
    Sign {
        /// Path to the .bin firmware file
        #[arg(long, value_name = "PATH")]
        file: PathBuf,
        /// Firmware version string (e.g. "1.2.3")
        #[arg(long, value_name = "VER")]
        version: String,
        /// Ed25519 private key file (base64-encoded 32-byte seed).
        /// Falls back to FIRMWARE_SIGN_KEY env var.
        #[arg(long, value_name = "KEY", env = "FIRMWARE_SIGN_KEY")]
        key: PathBuf,
        /// Optional key identifier for multi-key setups
        #[arg(long, value_name = "ID")]
        key_id: Option<String>,
        /// Output file path (default: metadata.json next to input)
        #[arg(long, value_name = "PATH", conflicts_with = "stdout")]
        output: Option<PathBuf>,
        /// Overwrite existing output file
        #[arg(long, conflicts_with = "stdout")]
        force: bool,
        /// Print signed JSON to stdout instead of writing a file
        #[arg(long)]
        stdout: bool,
    },
    /// Verify a signed metadata.json against a public key
    Verify {
        /// Path to signed metadata.json
        #[arg(long, value_name = "PATH")]
        metadata: PathBuf,
        /// Ed25519 public key file (base64-encoded 32 bytes)
        #[arg(long, value_name = "PATH")]
        pubkey: PathBuf,
        /// Optional: also verify the .bin file's SHA-256 and size
        #[arg(long, value_name = "PATH")]
        file: Option<PathBuf>,
    },
    /// Generate a new Ed25519 signing keypair
    Keygen {
        /// Output prefix (creates <prefix>.secret and <prefix>.pub)
        #[arg(long, value_name = "PREFIX", default_value = "firmware_key")]
        output: PathBuf,
    },
}

fn main() {
    if let Err(e) = run() {
        eprintln!("Error: {e}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Sign {
            file,
            version,
            key,
            key_id,
            output,
            force,
            stdout,
        } => cmd_sign(
            &file,
            &version,
            &key,
            key_id.as_deref(),
            output.as_deref(),
            force,
            stdout,
        ),
        Commands::Verify {
            metadata,
            pubkey,
            file,
        } => cmd_verify(&metadata, &pubkey, file.as_deref()),
        Commands::Keygen { output } => cmd_keygen(&output),
    }
}

// =========================================================================
// Subcommand implementations
// =========================================================================

fn cmd_sign(
    bin_path: &Path,
    version: &str,
    key_path: &Path,
    key_id: Option<&str>,
    output: Option<&Path>,
    force: bool,
    to_stdout: bool,
) -> Result<(), Box<dyn Error>> {
    validate_bin_extension(bin_path)?;
    let signing_key = load_signing_key(key_path)?;
    let (size, hash) = hash_file(bin_path)?;
    let name = file_name_from_path(bin_path)?;

    let canonical = build_canonical_payload(&name, size, &hash, version);
    let sig = signing_key.sign(canonical.as_bytes());
    let sig_b64 = BASE64.encode(sig.to_bytes());

    // Build output JSON (keys inserted alphabetically via serde_json::Map = BTreeMap)
    let mut map = serde_json::Map::new();
    map.insert("file_name".into(), serde_json::Value::String(name));
    map.insert("file_size_bytes".into(), serde_json::json!(size));
    if let Some(kid) = key_id {
        map.insert("key_id".into(), serde_json::Value::String(kid.into()));
    }
    map.insert("sha256_hash".into(), serde_json::Value::String(hash));
    map.insert(
        "signature".into(),
        serde_json::Value::String(sig_b64),
    );
    map.insert(
        "signing_alg".into(),
        serde_json::Value::String("ed25519".into()),
    );
    map.insert("version".into(), serde_json::Value::String(version.into()));

    let json_out = serde_json::to_string_pretty(&serde_json::Value::Object(map))?;

    if to_stdout {
        println!("{json_out}");
        return Ok(());
    }

    let dest = match output {
        Some(p) => p.to_path_buf(),
        None => metadata_output_path(bin_path),
    };
    if dest.exists() && !force {
        return Err(Box::new(io::Error::new(
            ErrorKind::AlreadyExists,
            format!(
                "'{}' already exists; use --force to overwrite",
                dest.display()
            ),
        )));
    }

    fs::write(&dest, json_out)?;
    eprintln!("Signed metadata written to {}", dest.display());
    Ok(())
}

fn cmd_verify(
    meta_path: &Path,
    pubkey_path: &Path,
    bin_path: Option<&Path>,
) -> Result<(), Box<dyn Error>> {
    let vk = load_verifying_key(pubkey_path)?;
    let raw = fs::read_to_string(meta_path)?;
    let doc: serde_json::Map<String, serde_json::Value> = serde_json::from_str(&raw)?;

    let str_field = |k: &str| -> Result<&str, String> {
        doc.get(k)
            .and_then(|v| v.as_str())
            .ok_or_else(|| format!("missing field '{k}'"))
    };

    let version = str_field("version")?;
    let file_name = str_field("file_name")?;
    let file_size = doc
        .get("file_size_bytes")
        .and_then(|v| v.as_u64())
        .ok_or("missing field 'file_size_bytes'")?;
    let sha256 = str_field("sha256_hash")?;
    let sig_b64 = str_field("signature")?;

    let canonical = build_canonical_payload(file_name, file_size, sha256, version);
    let sig_bytes = BASE64
        .decode(sig_b64)
        .map_err(|e| format!("invalid base64 in signature: {e}"))?;
    let sig =
        Signature::from_slice(&sig_bytes).map_err(|e| format!("invalid signature bytes: {e}"))?;

    vk.verify(canonical.as_bytes(), &sig)
        .map_err(|_| "signature verification FAILED — metadata may be tampered")?;
    eprintln!("✓ Ed25519 signature valid");

    if let Some(bp) = bin_path {
        let (actual_size, actual_hash) = hash_file(bp)?;
        if actual_size != file_size {
            return Err(
                format!("size mismatch: metadata={file_size}, file={actual_size}").into(),
            );
        }
        if actual_hash != sha256 {
            return Err(
                format!("SHA-256 mismatch:\n  metadata: {sha256}\n  actual:   {actual_hash}")
                    .into(),
            );
        }
        eprintln!("✓ Binary integrity verified (size + SHA-256)");
    }

    Ok(())
}

fn cmd_keygen(prefix: &Path) -> Result<(), Box<dyn Error>> {
    let secret_path = prefix.with_extension("secret");
    let pub_path = prefix.with_extension("pub");

    if secret_path.exists() || pub_path.exists() {
        return Err(format!(
            "key file(s) already exist: {}, {}",
            secret_path.display(),
            pub_path.display()
        )
        .into());
    }

    let sk = SigningKey::generate(&mut OsRng);
    let vk = sk.verifying_key();

    fs::write(&secret_path, BASE64.encode(sk.to_bytes()))?;
    fs::write(&pub_path, BASE64.encode(vk.to_bytes()))?;

    eprintln!("Keypair generated:");
    eprintln!("  Private: {}", secret_path.display());
    eprintln!("  Public:  {}", pub_path.display());
    Ok(())
}

// =========================================================================
// Canonical payload — deterministic JSON for signing
// =========================================================================

/// Build the canonical signing payload: compact JSON with alphabetically-sorted keys.
/// Both the Rust CLI and Python backend must produce identical output for the same inputs.
fn build_canonical_payload(
    file_name: &str,
    file_size_bytes: u64,
    sha256_hash: &str,
    version: &str,
) -> String {
    let mut m = BTreeMap::new();
    m.insert("file_name", serde_json::Value::String(file_name.into()));
    m.insert("file_size_bytes", serde_json::json!(file_size_bytes));
    m.insert(
        "sha256_hash",
        serde_json::Value::String(sha256_hash.into()),
    );
    m.insert("version", serde_json::Value::String(version.into()));
    serde_json::to_string(&m).expect("canonical JSON serialization cannot fail")
}

// =========================================================================
// Key I/O — base64-encoded 32-byte files
// =========================================================================

fn load_signing_key(path: &Path) -> Result<SigningKey, Box<dyn Error>> {
    let raw = fs::read_to_string(path)
        .map_err(|e| format!("cannot read private key '{}': {e}", path.display()))?;
    let bytes = BASE64
        .decode(raw.trim())
        .map_err(|e| format!("private key: invalid base64: {e}"))?;
    let arr: [u8; 32] = bytes
        .try_into()
        .map_err(|v: Vec<u8>| format!("private key must be 32 bytes, got {}", v.len()))?;
    Ok(SigningKey::from_bytes(&arr))
}

fn load_verifying_key(path: &Path) -> Result<VerifyingKey, Box<dyn Error>> {
    let raw = fs::read_to_string(path)
        .map_err(|e| format!("cannot read public key '{}': {e}", path.display()))?;
    let bytes = BASE64
        .decode(raw.trim())
        .map_err(|e| format!("public key: invalid base64: {e}"))?;
    let arr: [u8; 32] = bytes
        .try_into()
        .map_err(|v: Vec<u8>| format!("public key must be 32 bytes, got {}", v.len()))?;
    VerifyingKey::from_bytes(&arr).map_err(|e| format!("invalid public key: {e}").into())
}

// =========================================================================
// File helpers (preserved from v0.1)
// =========================================================================

fn validate_bin_extension(path: &Path) -> io::Result<()> {
    match path.extension().and_then(|ext| ext.to_str()) {
        Some(ext) if ext.eq_ignore_ascii_case("bin") => Ok(()),
        _ => Err(io::Error::new(
            ErrorKind::InvalidInput,
            format!("'{}' must have a .bin extension", path.display()),
        )),
    }
}

fn hash_file(path: &Path) -> io::Result<(u64, String)> {
    let file = File::open(path)?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut total = 0u64;
    let mut buf = [0u8; 8 * 1024];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        let n_u64 = u64::try_from(n)
            .map_err(|_| io::Error::new(ErrorKind::InvalidData, "read size conversion failed"))?;
        total = total
            .checked_add(n_u64)
            .ok_or_else(|| io::Error::other("file size overflow"))?;
    }
    Ok((total, format!("{:x}", hasher.finalize())))
}

fn file_name_from_path(path: &Path) -> io::Result<String> {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidInput, "path has no file name"))
}

fn metadata_output_path(input: &Path) -> PathBuf {
    let dir = match input.parent() {
        Some(p) if !p.as_os_str().is_empty() => p,
        _ => Path::new("."),
    };
    dir.join("metadata.json")
}

// =========================================================================
// Tests
// =========================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn test_hash() -> String {
        "a".repeat(64)
    }

    #[test]
    fn canonical_payload_deterministic() {
        let a = build_canonical_payload("fw.bin", 1024, &test_hash(), "1.0.0");
        let b = build_canonical_payload("fw.bin", 1024, &test_hash(), "1.0.0");
        assert_eq!(a, b);
    }

    #[test]
    fn canonical_keys_sorted_alphabetically() {
        let p = build_canonical_payload("z.bin", 99, &test_hash(), "0.1");
        let keys = ["file_name", "file_size_bytes", "sha256_hash", "version"];
        let positions: Vec<usize> = keys.iter().map(|k| p.find(k).unwrap()).collect();
        for w in positions.windows(2) {
            assert!(w[0] < w[1], "keys not sorted: {p}");
        }
    }

    #[test]
    fn canonical_payload_is_compact_json() {
        let p = build_canonical_payload("a.bin", 1, &test_hash(), "v");
        assert!(!p.contains(' '), "canonical payload must not contain spaces: {p}");
        assert!(!p.contains('\n'), "canonical payload must not contain newlines");
    }

    #[test]
    fn sign_verify_roundtrip() {
        let sk = SigningKey::generate(&mut OsRng);
        let vk = sk.verifying_key();
        let payload = build_canonical_payload("test.bin", 512, &test_hash(), "2.0.0");
        let sig = sk.sign(payload.as_bytes());
        assert!(vk.verify(payload.as_bytes(), &sig).is_ok());
    }

    #[test]
    fn tampered_version_fails() {
        let sk = SigningKey::generate(&mut OsRng);
        let vk = sk.verifying_key();
        let payload = build_canonical_payload("fw.bin", 100, &test_hash(), "1.0.0");
        let sig = sk.sign(payload.as_bytes());
        let tampered = build_canonical_payload("fw.bin", 100, &test_hash(), "9.9.9");
        assert!(vk.verify(tampered.as_bytes(), &sig).is_err());
    }

    #[test]
    fn tampered_hash_fails() {
        let sk = SigningKey::generate(&mut OsRng);
        let vk = sk.verifying_key();
        let payload = build_canonical_payload("fw.bin", 100, &test_hash(), "1.0.0");
        let sig = sk.sign(payload.as_bytes());
        let tampered = build_canonical_payload("fw.bin", 100, &"b".repeat(64), "1.0.0");
        assert!(vk.verify(tampered.as_bytes(), &sig).is_err());
    }

    #[test]
    fn wrong_key_fails() {
        let sk1 = SigningKey::generate(&mut OsRng);
        let sk2 = SigningKey::generate(&mut OsRng);
        let payload = build_canonical_payload("fw.bin", 256, &test_hash(), "1.0.0");
        let sig = sk1.sign(payload.as_bytes());
        assert!(sk2.verifying_key().verify(payload.as_bytes(), &sig).is_err());
    }

    #[test]
    fn bin_extension_accepted() {
        assert!(validate_bin_extension(Path::new("ok.bin")).is_ok());
        assert!(validate_bin_extension(Path::new("OK.BIN")).is_ok());
    }

    #[test]
    fn non_bin_extension_rejected() {
        assert!(validate_bin_extension(Path::new("bad.txt")).is_err());
        assert!(validate_bin_extension(Path::new("noext")).is_err());
    }
}
