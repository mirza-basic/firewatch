#!/bin/bash

api_key="REDACTED-ROTATE-THIS-KEY"
firms_url="https://firms.modaps.eosdis.nasa.gov"
area_api="/api/area/csv/$api_key/"
country_api="/api/country/csv/$api_key/"
data_availability_api="/api/data_availability/csv/$api_key/ALL"
date_today=$(date +"%Y-%m-%d")
#date_today="2024-02-18"
area="18,44.2,18.5,44.6"
country="BIH"
days_in_past="4"

fire_icon="🔥"
no_fire_message="No fire detected 😊"
fire_message="Fire detected!!!"

fire_detected=0

data_availability_url="$firms_url$data_availability_api"

# Function to update maximum column widths
update_max_widths() {
    source_arr+=("$1")
    location_arr+=("$2")
    date_arr+=("$3")
    time_arr+=("$4")
    daynight_arr+=("$5")
}

# Function to print dynamic table
print_dynamic_table() {
    # Calculate maximum widths for each column
    max_source_width=$(printf "%s\n" "${source_arr[@]}" | awk '{ print length }' | sort -rn | head -1)
    max_location_width=$(printf "%s\n" "${location_arr[@]}" | awk '{ print length }' | sort -rn | head -1)
    max_date_width=$(printf "%s\n" "${date_arr[@]}" | awk '{ print length }' | sort -rn | head -1)
    max_time_width=$(printf "%s\n" "${time_arr[@]}" | awk '{ print length }' | sort -rn | head -1)
    max_daynight_width=$(printf "%s\n" "${daynight_arr[@]}" | awk '{ print length }' | sort -rn | head -1)

    # Print table header
    printf "%-${max_source_width}s | %-${max_location_width}s | %-${max_date_width}s | %-${max_time_width}s | %-${max_daynight_width}s\n" "Source" "Location" "Date" "Time" "Day/Night"
    # Print divider
    printf "%-${max_source_width}s | %-${max_location_width}s | %-${max_date_width}s | %-${max_time_width}s | %-${max_daynight_width}s\n" "$(printf '%*s' "$max_source_width" | tr ' ' '-')" "$(printf '%*s' "$max_location_width" | tr ' ' '-')" "$(printf '%*s' "$max_date_width" | tr ' ' '-')" "$(printf '%*s' "$max_time_width" | tr ' ' '-')" "$(printf '%*s' "$max_daynight_width" | tr ' ' '-')"
    
    # Print table rows
    for ((i=0; i<${#source_arr[@]}; i++)); do
        printf "%-${max_source_width}s | %-${max_location_width}s | %-${max_date_width}s | %-${max_time_width}s | %-${max_daynight_width}s\n" "${source_arr[i]}" "${location_arr[i]}" "${date_arr[i]}" "${time_arr[i]}" "${daynight_arr[i]}"
    done
}

# Function to generate clickable Google Maps link
generate_google_maps_link() {
    local latitude=$1
    local longitude=$2
    echo "https://www.google.com/maps?q=$latitude,$longitude"
}

# Call the data availability API and store the response in a variable
data_availability_response=$(curl -s "$data_availability_url")

# Initialize arrays to store column data for dynamic formatting
source_arr=()
location_arr=()
date_arr=()
time_arr=()
daynight_arr=()

# Iterate through each line of the data availability response
while IFS= read -r line; do
    # Extract the data_id and use it to construct the area URL
    data_id=$(echo "$line" | cut -d',' -f1)
    areaurl="$firms_url$area_api$data_id/$area/$days_in_past/$date_today"
    countryurl="$firms_url$country_api$data_id/$country/$days_in_past/$date_today"

    # Call the area API and store the response in a variable
    area_response=$(curl -s "$countryurl" | tail -n +2)
    # Check if area response contains valid data
    if [[ $(echo "$area_response" | wc -l) -gt 1 ]]; then
        # Convert the area response from CSV to JSON and iterate through each line
        while IFS= read -r area_line; do
            # Extract the required values from the CSV line
            latitude=$(echo "$area_line" | cut -d',' -f2)
            longitude=$(echo "$area_line" | cut -d',' -f3)
            acq_date=$(echo "$area_line" | cut -d',' -f7)
            acq_time=$(echo "$area_line" | cut -d',' -f8)
            daynight=$(echo "$area_line" | cut -d',' -f15)
            
            # Update maximum column widths
            update_max_widths "$data_id" "$(generate_google_maps_link $latitude $longitude)" "$acq_date" "$acq_time" "$daynight"
            
            fire_detected=1
        done <<< "$area_response"
    fi
done <<< "$data_availability_response"

# Print fire message if detected
if [[ $fire_detected -eq 1 ]]; then
    echo "$fire_message $fire_icon"
    # Print dynamic table
    print_dynamic_table
else
    echo "$no_fire_message"
fi
