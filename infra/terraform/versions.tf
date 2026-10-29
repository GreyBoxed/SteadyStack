terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    cloudinit = {
      source  = "hashicorp/cloudinit"
      version = "~> 2.3"
    }
  }

  # State holds the instance id, the elastic IP and every ARN -- not the
  # secrets, which is the point of §M48 -- but it is still the map of the
  # whole system and belongs somewhere versioned and locked rather than on a
  # laptop. The bucket and table have to exist before this can point at them,
  # which is the one bootstrap step Terraform cannot do for itself; see
  # README.md. Left commented so a first `init` works locally.
  #
  # backend "s3" {
  #   bucket       = "steadystack-tfstate"
  #   key          = "steadystack/terraform.tfstate"
  #   region       = "ap-south-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "steadystack"
      ManagedBy = "terraform"
      # M47: nothing here is changed in the console. A resource without this
      # tag is a resource somebody made by hand, and that is worth being able
      # to find.
      Repository = "steadystack"
    }
  }
}

# Route 53 publishes health-check metrics in us-east-1 and nowhere else, so the
# alarm on the outside probe -- and the topic it notifies -- live there
# (observability.tf).
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project    = "steadystack"
      ManagedBy  = "terraform"
      Repository = "steadystack"
    }
  }
}
