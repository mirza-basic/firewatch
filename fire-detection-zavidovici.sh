#!/bin/bash

api_key="REDACTED-ROTATE-THIS-KEY"
firms_url="https://firms.modaps.eosdis.nasa.gov"
area_api="/api/area/csv/$api_key/"
country_api="/api/country/csv/$api_key/"
data_availability_api="/api/data_availability/csv/$api_key/ALL"
date_today=$(date +"%Y-%m-%d")
#date_today="2021-07-14"
# Bounding box of Grad Zavidovici (west,south,east,north) - used to query FIRMS,
# which only supports rectangular areas. Results are then clipped to the polygon below.
area="18.059,44.255,18.493,44.517"
days_in_past="1"

# Administrative boundary of Grad (opstina) Zavidovici.
# Source: OpenStreetMap relation 2528292 (boundary=administrative, admin_level=6),
# simplified with Douglas-Peucker at 0.0005 deg (~55 m, area error 0.01%).
# Format: "lon lat" pairs, ring is closed (last point == first point).
zavidovici_polygon="
18.05988 44.34851 18.07242 44.33506 18.07332 44.33058 18.07242 44.32224 18.06794 44.31840
18.06884 44.31455 18.07153 44.31007 18.07586 44.29022 18.08625 44.29272 18.08738 44.29463
18.09197 44.29666 18.09607 44.29971 18.10252 44.30015 18.10581 44.29649 18.10764 44.29552
18.11164 44.29505 18.13110 44.29572 18.13781 44.29340 18.14229 44.28955 18.14408 44.28635
18.14360 44.27961 18.15159 44.27364 18.15330 44.27008 18.15761 44.26730 18.16259 44.26670
18.16386 44.26782 18.16632 44.26668 18.17027 44.26780 18.17164 44.26708 18.17398 44.26743
18.17482 44.26834 18.17654 44.26839 18.17989 44.26701 18.18177 44.26704 18.18266 44.26800
18.18249 44.26705 18.18348 44.26680 18.18653 44.26809 18.18702 44.26646 18.19633 44.26688
18.20252 44.26423 18.20687 44.26405 18.20732 44.26158 18.20822 44.26084 18.21228 44.26138
18.21754 44.26005 18.22829 44.26069 18.24173 44.25556 18.24710 44.25556 18.25427 44.25748
18.26860 44.26710 18.27397 44.26710 18.28204 44.26903 18.28831 44.27288 18.29279 44.27416
18.30085 44.28121 18.30622 44.28442 18.31160 44.28891 18.32951 44.29404 18.33310 44.29404
18.33937 44.29019 18.34385 44.29340 18.34922 44.29404 18.35459 44.29212 18.36624 44.28442
18.37520 44.28635 18.37968 44.28891 18.39670 44.29404 18.41193 44.29276 18.42715 44.29917
18.43074 44.30237 18.43701 44.31007 18.45224 44.31007 18.45672 44.31199 18.46388 44.31648
18.46657 44.31904 18.46657 44.32289 18.48538 44.32353 18.49295 44.32559 18.48981 44.33072
18.48623 44.33424 18.47727 44.34033 18.47234 44.34706 18.46115 44.35186 18.45443 44.34898
18.44457 44.35314 18.43965 44.35250 18.43338 44.34802 18.42531 44.35154 18.42084 44.35250
18.41277 44.35026 18.40426 44.34898 18.40202 44.35026 18.40023 44.35442 18.39889 44.35570
18.39934 44.35731 18.40740 44.36275 18.39755 44.36883 18.39665 44.37011 18.41277 44.38004
18.41636 44.38004 18.42352 44.37556 18.42943 44.37865 18.42576 44.38670 18.43284 44.38931
18.43427 44.39188 18.40561 44.40404 18.39889 44.40532 18.39351 44.41044 18.39341 44.41378
18.39069 44.41470 18.38391 44.41941 18.37938 44.42104 18.36888 44.41492 18.36037 44.41876
18.34702 44.41995 18.33977 44.42196 18.33394 44.42580 18.32632 44.42882 18.31866 44.42976
18.31832 44.44110 18.31737 44.44307 18.32051 44.44499 18.31530 44.45161 18.31411 44.45669
18.31468 44.46353 18.31827 44.46481 18.32679 44.46578 18.32410 44.46738 18.31962 44.47601
18.31738 44.48240 18.31470 44.48624 18.31156 44.48879 18.31022 44.49614 18.30753 44.49902
18.30171 44.50828 18.29275 44.51020 18.28333 44.51419 18.28132 44.51646 18.27993 44.51663
18.27847 44.51651 18.27686 44.51487 18.27147 44.51205 18.26618 44.51152 18.25985 44.50881
18.25098 44.50881 18.25668 44.50339 18.25541 44.50022 18.25161 44.49842 18.24464 44.49254
18.24021 44.49209 18.22691 44.49616 18.21677 44.49616 18.20537 44.49480 18.19650 44.49209
18.19334 44.49209 18.18700 44.49480 18.18510 44.49751 18.17399 44.50287 18.16650 44.49305
18.15860 44.48803 18.15435 44.48739 18.14626 44.48796 18.14039 44.48664 18.13822 44.48451
18.13729 44.47978 18.14077 44.47690 18.13932 44.47584 18.13913 44.47412 18.14002 44.47356
18.14140 44.47400 18.14235 44.47252 18.14175 44.47089 18.14329 44.47040 18.14485 44.46800
18.14479 44.46526 18.14353 44.46223 18.13717 44.45722 18.13229 44.45736 18.12629 44.45880
18.12683 44.45594 18.12359 44.45234 18.11657 44.45077 18.11400 44.44761 18.11157 44.44836
18.11061 44.44780 18.11071 44.44682 18.10630 44.44488 18.10969 44.44213 18.11072 44.43885
18.11506 44.43665 18.11503 44.43297 18.11879 44.43098 18.12613 44.43050 18.12594 44.42781
18.13144 44.42400 18.13102 44.42124 18.12851 44.41821 18.12479 44.41697 18.12245 44.41777
18.12155 44.41698 18.12022 44.41722 18.11964 44.41626 18.12149 44.41537 18.11842 44.41348
18.12038 44.41241 18.12175 44.40953 18.12502 44.40840 18.12324 44.40713 18.12371 44.40589
18.12121 44.40311 18.12377 44.40044 18.12438 44.39782 18.12706 44.39718 18.12706 44.39334
18.11184 44.38630 18.10109 44.38310 18.08586 44.37989 18.08138 44.37669 18.07063 44.36325
18.07332 44.35940 18.07332 44.35748 18.05988 44.34851
"

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

# Function to test whether a point falls inside the Zavidovici boundary
# (ray casting / even-odd rule)
inside_zavidovici() {
    local latitude=$1
    local longitude=$2
    echo "$zavidovici_polygon" | awk -v lat="$latitude" -v lon="$longitude" '
        { for (i = 1; i <= NF; i += 2) { n++; x[n] = $i; y[n] = $(i+1) } }
        END {
            inside = 0
            for (i = 1; i < n; i++) {
                j = i + 1
                if ((y[i] > lat) != (y[j] > lat)) {
                    xint = x[i] + (lat - y[i]) * (x[j] - x[i]) / (y[j] - y[i])
                    if (lon < xint) inside = !inside
                }
            }
            exit inside ? 0 : 1
        }'
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

    # Call the area API and store the response in a variable
    area_response=$(curl -s "$areaurl" | tail -n +2)
    # Check if area response contains valid data
    if [[ $(echo "$area_response" | wc -l) -gt 1 ]]; then
        # Convert the area response from CSV to JSON and iterate through each line
        while IFS= read -r area_line; do
            # Extract the required values from the CSV line
            latitude=$(echo "$area_line" | cut -d',' -f1)
            longitude=$(echo "$area_line" | cut -d',' -f2)
            acq_date=$(echo "$area_line" | cut -d',' -f6)
            acq_time=$(echo "$area_line" | cut -d',' -f7)
            daynight=$(echo "$area_line" | cut -d',' -f14)

            # Skip detections that fall inside the bounding box but outside the municipality
            if ! inside_zavidovici "$latitude" "$longitude"; then
                continue
            fi

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
