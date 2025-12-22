variable "project_id" {}
variable "region" {}

resource "google_compute_network" "vpc" {
  name                    = "jarvis-vpc"
  auto_create_subnetworks = false
  project                 = var.project_id
}

resource "google_compute_subnetwork" "subnet" {
  name          = "jarvis-subnet-01"
  ip_cidr_range = "10.0.0.0/24"
  region        = var.region
  network       = google_compute_network.vpc.id
  project       = var.project_id
}

# Allow internal traffic between instances
resource "google_compute_firewall" "allow_internal" {
  name    = "jarvis-allow-internal"
  network = google_compute_network.vpc.name
  project = var.project_id

  allow {
    protocol = "icmp"
  }

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }

  source_ranges = ["10.0.0.0/24"]
}

# Allow SSH (from IAP range or everywhere for now since Spot VMs need it)
resource "google_compute_firewall" "allow_ssh" {
  name    = "jarvis-allow-ssh"
  network = google_compute_network.vpc.name
  project = var.project_id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"] # Open to world for simplicity with Spot VMs, restricted in prod
  target_tags   = ["jarvis-node"]
}

# Allow HTTP/HTTPS for API
resource "google_compute_firewall" "allow_web" {
  name    = "jarvis-allow-web"
  network = google_compute_network.vpc.name
  project = var.project_id

  allow {
    protocol = "tcp"
    ports    = ["80", "443", "8000", "8080"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["jarvis-node"]
}

output "vpc_id" {
  value = google_compute_network.vpc.id
}

output "subnet_id" {
  value = google_compute_subnetwork.subnet.id
}

